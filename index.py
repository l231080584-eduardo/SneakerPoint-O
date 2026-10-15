
from functools import wraps
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import logging
import math
import os
import re
import unicodedata
from urllib.parse import urlsplit

import mercadopago
import psycopg2
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash, generate_password_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or os.urandom(32)

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")


DB_ENGINE = os.getenv("DB_ENGINE", "postgres").strip().lower()
DB_DRIVER = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")
PRODUCT_STYLES = ("Deportivo", "Casual", "Moda", "Infantil", "Otro")
if DB_ENGINE not in {"postgres", "sqlserver"}:
    raise ValueError("DB_ENGINE debe ser 'postgres' o 'sqlserver'.")

MP_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN", "").strip()
MP_PUBLIC_KEY = os.getenv("MERCADOPAGO_PUBLIC_KEY", "")
MP_WEBHOOK_SECRET = os.getenv("MERCADOPAGO_WEBHOOK_SECRET", "")
MP_BASE_URL = os.getenv("MERCADOPAGO_BASE_URL", "").strip().rstrip("/")
if MP_BASE_URL:
    parsed_mp_base_url = urlsplit(MP_BASE_URL)
    if parsed_mp_base_url.scheme != "https" or not parsed_mp_base_url.netloc:
        raise ValueError("MERCADOPAGO_BASE_URL debe ser una URL pública HTTPS.")

mp_sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None


def get_conn():
    if DB_ENGINE == "sqlserver":
        try:
            import pyodbc
        except ImportError as error:
            raise RuntimeError("DB_ENGINE=sqlserver requiere instalar pyodbc desde requirements.txt.") from error

        server = DB_HOST or "localhost"
        if DB_PORT:
            server = f"{server},{DB_PORT}"

        def odbc_value(value):
            return "{" + str(value or "").replace("}", "}}") + "}"

        connection_string = (
            f"DRIVER={odbc_value(DB_DRIVER)};"
            f"SERVER={odbc_value(server)};"
            f"DATABASE={odbc_value(DB_NAME)};"
            f"UID={odbc_value(DB_USER)};"
            f"PWD={odbc_value(DB_PASS)};"
            f"TrustServerCertificate={os.getenv('DB_TRUST_SERVER_CERTIFICATE', 'yes')};"
        )
        return pyodbc.connect(connection_string)
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url, sslmode=os.getenv("DB_SSLMODE", "require"))
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
    )


def _sqlserver_sql(sql):
    sql = re.sub(r"\b([A-Za-z_][\w.]*)\s+ILIKE\s+(%s)", r"LOWER(\1) LIKE LOWER(\2)", sql, flags=re.IGNORECASE)
    sql = re.sub(
        r"STRING_AGG\s*\(\s*(?:DISTINCT\s+)?([^,]+),\s*('[^']*')\s+ORDER BY\s+([^)]+)\)",
        r"STRING_AGG(\1, \2) WITHIN GROUP (ORDER BY \3)",
        sql,
        flags=re.IGNORECASE,
    )

    returning = re.search(
        r"\bINSERT\s+INTO\s+([\w.]+)\s*(\([^)]*\))\s*VALUES\s*(\([^)]*\))\s*RETURNING\s+(\w+)",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if returning:
        sql = (
            f"INSERT INTO {returning.group(1)} {returning.group(2)} "
            f"OUTPUT INSERTED.{returning.group(4)} VALUES {returning.group(3)}"
        )

    if re.search(r"\bFOR\s+UPDATE\b", sql, flags=re.IGNORECASE):
        for table in ("productos", "productos_proveedor", "pedidos"):
            sql = re.sub(
                rf"\b(FROM|JOIN)\s+{table}\b(?!\s+WITH\b)(?=\s+(?:WHERE|FOR\s+UPDATE))",
                rf"\1 {table} WITH (UPDLOCK, ROWLOCK)",
                sql,
                flags=re.IGNORECASE,
            )
    sql = re.sub(r"\s+FOR\s+UPDATE\b", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bTRUE\b", "1", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bFALSE\b", "0", sql, flags=re.IGNORECASE)

    limit = re.search(r"\bLIMIT\s+(\d+)\s*$", sql, flags=re.IGNORECASE)
    if limit:
        if not re.search(r"\bORDER\s+BY\b", sql, flags=re.IGNORECASE):
            raise ValueError("Las consultas SQL Server con LIMIT requieren ORDER BY.")
        sql = sql[:limit.start()].rstrip() + f" OFFSET 0 ROWS FETCH NEXT {limit.group(1)} ROWS ONLY"

    if re.search(r"UPDATE\s+productos\s+\w+\s+SET\s+stock\s*=", sql, flags=re.IGNORECASE):
        sql = re.sub(r"UPDATE\s+productos\s+\w+\s+SET\s+", "UPDATE p SET ", sql, count=1, flags=re.IGNORECASE)
        sql = re.sub(r"\bFROM\s*\(", "FROM productos AS p JOIN (", sql, count=1, flags=re.IGNORECASE)
        sql = re.sub(r"\)\s+lines\s+WHERE\s+p\.id_producto\s*=\s*lines\.id_producto", ") AS lines ON p.id_producto = lines.id_producto", sql, flags=re.IGNORECASE)
    return sql


def _execute(cur, sql, params=None):
    if DB_ENGINE == "sqlserver":
        sql = _sqlserver_sql(sql).replace("%s", "?")
    return cur.execute(sql, params or ())


def _row_as_dict(cur, row):
    if row is None or hasattr(row, "keys"):
        return row
    return dict(zip((column[0] for column in cur.description), row))


def _fetchone(cur):
    return _row_as_dict(cur, cur.fetchone())


def _fetchall(cur):
    return [_row_as_dict(cur, row) for row in cur.fetchall()]


def _cursor(conn):
    if DB_ENGINE == "postgres":
        return conn.cursor(cursor_factory=RealDictCursor)
    return conn.cursor()


def _database_error_types():
    if DB_ENGINE == "sqlserver":
        try:
            import pyodbc
        except ImportError:
            return ()
        return (pyodbc.Error,)
    return (psycopg2.Error,)


def _is_integrity_error(error):
    if DB_ENGINE == "sqlserver":
        try:
            import pyodbc
        except ImportError:
            return False
        return isinstance(error, pyodbc.IntegrityError)
    return isinstance(error, psycopg2.IntegrityError)


def _db_query(sql, params=None, fetch_one=False, fetch_all=False, commit=False):
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = _cursor(conn)
        _execute(cur, sql, params)
        if commit:
            conn.commit()
        elif fetch_one:
            return _fetchone(cur)
        elif fetch_all:
            return _fetchall(cur)
        return None
    except _database_error_types():
        if conn:
            conn.rollback()
        logging.exception("Database operation failed")
        raise
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def role_required(role, login_endpoint):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("loggedin") or session.get("role") != role:
                if request.accept_mimetypes.best == "application/json":
                    return jsonify({"error": "Tu sesión expiró. Inicia sesión y vuelve a intentar el pago."}), 401
                flash("Inicia sesión para acceder a esta sección.", "warning")
                return redirect(url_for(login_endpoint, next=request.path))
            if role == "provider":
                try:
                    account = _db_query(
                        "SELECT activo FROM proveedores WHERE id_proveedor=%s",
                        (session.get("id_proveedor"),), fetch_one=True,
                    )
                except Exception:
                    account = None
                if not account or not account["activo"]:
                    session.clear()
                    if request.accept_mimetypes.best == "application/json":
                        return jsonify({"error": "El acceso de proveedor no está activo."}), 403
                    flash("El acceso de proveedor no está activo.", "warning")
                    return redirect(url_for("provider_login"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


customer_required = role_required("customer", "login")
admin_required = role_required("admin", "admin_login")
provider_required = role_required("provider", "provider_login")


def password_matches(stored_hash, password):
    if not stored_hash:
        return False, False
    if stored_hash.startswith(("pbkdf2:", "scrypt:")):
        try:
            return check_password_hash(stored_hash, password), False
        except (TypeError, ValueError):
            return False, False
    legacy_hash = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(stored_hash, legacy_hash), True


def is_valid_password(password):
    return (
        8 <= len(password) <= 20
        and any(character.isupper() for character in password)
        and any(character.islower() for character in password)
        and any(character.isdigit() for character in password)
        and any(not character.isalnum() and not character.isspace() for character in password)
    )


def safe_next(default_endpoint):
    next_path = request.form.get("next") or request.args.get("next")
    parsed = urlsplit(next_path or "")
    if next_path and next_path.startswith("/") and not next_path.startswith("//") and not parsed.netloc and "\\" not in next_path:
        return next_path
    return url_for(default_endpoint)


def _cart_details():
    cart = session.get("cart", {})
    if not cart:
        return [], 0
    product_ids = []
    for key in cart:
        try:
            product_ids.append(int(key.split("_", 1)[0]))
        except (TypeError, ValueError):
            continue
    if not product_ids:
        session["cart"] = {}
        return [], 0
    marks = ", ".join(["%s"] * len(set(product_ids)))
    products = _db_query(
        f"SELECT * FROM productos WHERE id_producto IN ({marks}) AND activo = TRUE",
        tuple(sorted(set(product_ids))),
        fetch_all=True,
    )
    by_id = {product["id_producto"]: product for product in products}
    items = []
    total = 0
    for key, quantity in cart.items():
        try:
            product_id_text, size = key.split("_", 1)
            product = by_id.get(int(product_id_text))
            quantity = int(quantity)
        except (TypeError, ValueError):
            continue
        if product and quantity > 0:
            subtotal = product["precio"] * quantity
            items.append({"producto": product, "cantidad": quantity, "talla_seleccionada": size, "subtotal": subtotal, "cart_key": key})
            total += subtotal
    return items, total


def _is_child_shoe(product):
    text = " ".join(
        str(product.get(field) or "")
        for field in ("nombre", "descripcion", "estilo")
    )
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFD", text.lower())
        if not unicodedata.combining(character)
    )
    return any(term in normalized for term in ("infantil", "nino", "nina", "kids", "junior", "juvenil", "child"))


def _shoe_size_options(child_sized):
    half_sizes = range(47, 23, -1) if child_sized else range(60, 47, -1)
    return [
        f"{size // 2}{'.5' if size % 2 else ''}"
        for size in half_sizes
    ]


def _locked_cart_items(cur):
    cart_lines = []
    product_totals = {}
    for key, raw_quantity in session.get("cart", {}).items():
        try:
            product_id_text, size = key.split("_", 1)
            product_id = int(product_id_text)
            quantity = int(raw_quantity)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("El carrito contiene un artículo inválido.")
        if product_id < 1 or quantity < 1:
            raise ValueError("El carrito contiene una cantidad inválida.")
        cart_lines.append((product_id, size, quantity))
        product_totals[product_id] = product_totals.get(product_id, 0) + quantity

    products = {}
    for product_id, requested_quantity in sorted(product_totals.items()):
        _execute(
            cur,
            "SELECT id_producto, nombre, descripcion, estilo, precio, stock FROM productos WHERE id_producto = %s AND activo = TRUE FOR UPDATE",
            (product_id,),
        )
        product = _fetchone(cur)
        if not product or product["stock"] < requested_quantity:
            raise ValueError(
                f"No hay stock suficiente para {product['nombre'] if product else 'uno de los modelos'}."
            )
        products[product_id] = product

    locked_items = []
    for product_id, size, quantity in cart_lines:
        product = products[product_id]
        if size not in _shoe_size_options(_is_child_shoe(product)):
            raise ValueError(f"La talla seleccionada no es válida para {product['nombre']}.")
        locked_items.append((product, quantity, size))
    return locked_items


def _restore_order_stock(cur, order_id):
    _execute(
        cur,
        """SELECT id_producto, SUM(cantidad) AS cantidad FROM ventas
           WHERE id_pedido=%s GROUP BY id_producto""",
        (order_id,),
    )
    for item in _fetchall(cur):
        _execute(
            cur,
            "UPDATE productos SET stock = stock + %s WHERE id_producto = %s",
            (item["cantidad"], item["id_producto"]),
        )


@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/")
@app.route("/index")
def index():
    query = request.args.get("q", "").strip()
    try:
        if query:
            term = f"%{query}%"
            productos = _db_query(
                """SELECT * FROM productos WHERE activo = TRUE
                   AND (marca ILIKE %s OR nombre ILIKE %s OR descripcion ILIKE %s OR estilo ILIKE %s)
                   ORDER BY nombre""",
                (term, term, term, term), fetch_all=True,
            )
        else:
            productos = _db_query("SELECT * FROM productos WHERE activo = TRUE ORDER BY nombre", fetch_all=True)
    except Exception:
        productos = []
        flash("El catálogo no está disponible por el momento.", "warning")
    pending_favorites = []
    if session.get("loggedin") and session.get("role") == "customer":
        pending_favorites = session.pop("pending_favorites", [])
    return render_template(
        "index.html",
        productos=productos,
        search_query=query,
        pending_favorites=pending_favorites,
        size_guide_adult=_CHATBOT_ADULT_SIZE_GUIDE,
        size_guide_children=_CHATBOT_CHILD_SIZE_GUIDE,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("correo", "").strip().lower()
        password = request.form.get("contraseña", "")
        try:
            customer = _db_query(
                "SELECT id_clientes, nombre, contraseña FROM clientes WHERE LOWER(correo) = %s",
                (email,), fetch_one=True,
            )
        except Exception:
            customer = None
        valid, legacy = password_matches(customer["contraseña"], password) if customer else (False, False)
        if customer and valid:
            if legacy:
                _db_query("UPDATE clientes SET contraseña = %s WHERE id_clientes = %s", (generate_password_hash(password), customer["id_clientes"]), commit=True)
            pending_items = session.pop("pending_items", {})
            pending_favorites = session.pop("pending_favorites", [])
            session.clear()
            session.update({"loggedin": True, "role": "customer", "id_cliente": customer["id_clientes"], "nombre_cliente": customer["nombre"]})
            if pending_items:
                session["cart"] = pending_items
            if pending_favorites:
                session["pending_favorites"] = pending_favorites
            flash(f"Hola, {customer['nombre']}. Ya puedes continuar.", "success")
            return redirect(safe_next("index"))
        flash("Correo o contraseña incorrectos.", "error")
    return render_template("login.html")


@app.route("/registro", methods=["GET", "POST"])
def registro():
    if request.method == "POST":
        print("========== POST /registro ==========")
        print("DATOS RECIBIDOS:", request.form.to_dict())

        nombre = request.form.get("nombre", "").strip()
        apellido = request.form.get("apellido", "").strip()
        correo = request.form.get("correo", "").strip().lower()
        telefono = request.form.get("telefono", "").strip()
        password = request.form.get("contraseña", "")

        print("nombre:", nombre)
        print("apellido:", apellido)
        print("correo:", correo)
        print("telefono:", telefono)
        print("contraseña recibida:", bool(password))

        if not all([nombre, apellido, correo, telefono, password]):
            print("ERROR: FALTA ALGÚN CAMPO")
            flash("Completa todos los campos.", "error")
            return render_template("registro.html")
        if not is_valid_password(password):
            flash("La contraseña debe tener entre 8 y 20 caracteres e incluir mayúscula, minúscula, número y símbolo.", "error")
            return render_template("registro.html")

        try:
            print("Intentando generar hash...")
            password_hash = generate_password_hash(password)
            print("Hash generado correctamente.")

            print("Intentando INSERT en clientes...")

            _db_query(
                """
                INSERT INTO clientes
                (nombre, apellido, correo, contraseña, telefono)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (nombre, apellido, correo, password_hash, telefono),
                commit=True,
            )

            print("USUARIO INSERTADO CORRECTAMENTE")

            flash("Cuenta creada. Inicia sesión para comprar.", "success")
            return redirect(url_for("login"))

        except Exception as e:
            if _is_integrity_error(e):
                print("ERROR: CORREO DUPLICADO O RESTRICCIÓN DE BASE DE DATOS")
                logging.exception("ERROR DE INTEGRIDAD AL REGISTRAR USUARIO")
                flash("Ese correo ya tiene una cuenta.", "error")
                return render_template("registro.html")
            print("ERROR GENERAL AL REGISTRAR:", repr(e))
            logging.exception("ERROR AL REGISTRAR USUARIO")
            flash(f"Error: {e}", "error")

    return render_template("registro.html")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form.get("correo", "").strip().lower()
        password = request.form.get("contraseña", "")
        configured_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
        password_hash = os.getenv("ADMIN_PASSWORD_HASH", "")
        try:
            valid = bool(configured_email and email == configured_email and password_hash and check_password_hash(password_hash, password))
        except (TypeError, ValueError):
            valid = False
        if valid:
            session.clear()
            session.update({"loggedin": True, "role": "admin", "nombre_admin": "Administración"})
            return redirect(url_for("admin_dashboard"))
        flash("Credenciales administrativas incorrectas o no configuradas.", "error")
    return render_template("admin_login.html")


@app.route("/proveedores")
def provider_landing():
    return render_template("proveedores.html")


@app.route("/proveedor/login", methods=["GET", "POST"])
def provider_login():
    if request.method == "POST":
        email = request.form.get("correo", "").strip().lower()
        password = request.form.get("contraseña", "")
        try:
            provider = _db_query(
                "SELECT id_proveedor, razon_social, password_hash, activo FROM proveedores WHERE LOWER(correo) = %s",
                (email,), fetch_one=True,
            )
        except Exception:
            provider = None
        valid, legacy = password_matches(provider["password_hash"], password) if provider else (False, False)
        if provider and valid and provider["activo"]:
            if legacy:
                _db_query("UPDATE proveedores SET password_hash = %s WHERE id_proveedor = %s", (generate_password_hash(password), provider["id_proveedor"]), commit=True)
            session.clear()
            session.update({"loggedin": True, "role": "provider", "id_proveedor": provider["id_proveedor"], "nombre_proveedor": provider["razon_social"]})
            return redirect(safe_next("provider_dashboard"))
        flash("Credenciales incorrectas o cuenta pendiente de autorización administrativa.", "error")
    return render_template("provider_login.html")


@app.route("/proveedor/registro", methods=["GET", "POST"])
def provider_register():
    if request.method == "POST":
        name = request.form.get("razon_social", "").strip()
        contact = request.form.get("contacto", "").strip()
        email = request.form.get("correo", "").strip().lower()
        phone = request.form.get("telefono", "").strip()
        location = request.form.get("localidad", "").strip()
        password = request.form.get("contraseña", "")
        if not all([name, contact, email, phone, password]):
            flash("Completa los datos requeridos.", "error")
            return render_template("provider_register.html")
        if not is_valid_password(password):
            flash("La contraseña debe tener entre 8 y 20 caracteres e incluir mayúscula, minúscula, número y símbolo.", "error")
            return render_template("provider_register.html")
        try:
            _db_query(
                """INSERT INTO proveedores (razon_social, contacto, correo, telefono, localidad, password_hash, activo)
                   VALUES (%s, %s, %s, %s, %s, %s, FALSE)""",
                (name, contact, email, phone, location, generate_password_hash(password)), commit=True,
            )
            flash("Solicitud recibida. Administración revisará y habilitará tu cuenta.", "success")
            return redirect(url_for("provider_login"))
        except Exception as error:
            if _is_integrity_error(error):
                flash("Ese correo ya está registrado como proveedor.", "error")
            else:
                logging.exception("Could not submit provider registration")
                flash("No se pudo enviar tu solicitud.", "error")
    return render_template("provider_register.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("index"))


@app.route("/search")
def search_products():
    return redirect(url_for("index", q=request.args.get("q", "")))


@app.route("/add_favorite", methods=["POST"])
def add_favorite():
    try:
        product_id = int(request.form.get("id_producto", ""))
        product = _db_query(
            "SELECT id_producto FROM productos WHERE id_producto = %s AND activo = TRUE",
            (product_id,),
            fetch_one=True,
        )
    except (TypeError, ValueError):
        flash("No se pudo identificar este modelo.", "error")
        return redirect(url_for("index"))
    except Exception:
        logging.exception("Could not verify product for favorites")
        flash("No se pudo guardar este modelo en favoritos.", "error")
        return redirect(url_for("index"))
    if not product:
        flash("Este modelo ya no está disponible.", "error")
        return redirect(url_for("index"))

    pending_favorites = session.get("pending_favorites", [])
    if not isinstance(pending_favorites, list):
        pending_favorites = []
    pending_favorites = list(dict.fromkeys(
        str(favorite_id)
        for favorite_id in pending_favorites
        if str(favorite_id).isdigit()
    ))
    favorite_id = str(product_id)
    if favorite_id not in pending_favorites:
        pending_favorites.append(favorite_id)
    session["pending_favorites"] = pending_favorites

    if not session.get("loggedin") or session.get("role") != "customer":
        flash("Inicia sesión para guardar este modelo en favoritos.", "warning")
        return redirect(url_for("login", next=url_for("index")))
    return redirect(url_for("index"))


@app.route("/favoritos")
@customer_required
def favoritos():
    catalog_available = True
    try:
        productos = _db_query(
            "SELECT * FROM productos WHERE activo = TRUE ORDER BY nombre",
            fetch_all=True,
        )
    except Exception:
        logging.exception("Could not load products for favorites")
        productos = []
        catalog_available = False
        flash("No se pudo cargar el catálogo de favoritos.", "warning")
    return render_template("favoritos.html", productos=productos, catalog_available=catalog_available)


_CHATBOT_STOP_WORDS = {
    "a", "al", "algo", "algun", "alguna", "algunas", "algunos", "algun", "ayuda",
    "busca", "busco", "buscar", "buscando", "catalogo", "consulta", "consultar",
    "con", "de", "del", "disponible", "disponibles", "donde", "el", "ella", "ellos",
    "en", "es", "ese", "esa", "estos", "estas", "favor", "hay", "hola", "buenas",
    "buenos", "dias", "tardes", "noches", "la", "las", "lo",
    "los", "mas", "me", "mi", "mis", "modelo", "modelos", "mostrar", "muestra",
    "necesito", "para", "por", "precio", "precios", "puedes", "que", "quiero", "recomienda", "recomendar",
    "barato", "baratos", "barata", "baratas", "economico", "economicos", "economica", "economicas",
    "caro", "caros", "cara", "caras", "color", "colores", "talla", "tallas", "cuanto", "cuesta",
    "menos", "menor", "hasta", "maximo",
    "saber", "tenis", "tienen", "tienes", "un", "una", "unas", "unos", "ver", "y",
    "cuestan", "costar", "costaria", "costarian", "serian", "salen", "sale",
}

_CHATBOT_ADULT_SIZE_GUIDE = (
    ("24", "38", "6", "7.5", "23.5–24"),
    ("24.5", "39", "6.5", "8", "24–24.5"),
    ("25", "40", "7", "8.5", "24.5–25"),
    ("25.5", "40.5", "7.5", "9", "25–25.5"),
    ("26", "41", "8", "9.5", "25.5–26"),
    ("26.5", "42", "8.5", "10", "26–26.5"),
    ("27", "42.5", "9", "10.5", "26.5–27"),
    ("27.5", "43", "9.5", "11", "27–27.5"),
    ("28", "44", "10", "11.5", "27.5–28"),
    ("28.5", "44.5", "10.5", "12", "28–28.5"),
    ("29", "45", "11", "12.5", "28.5–29"),
    ("29.5", "45.5", "11.5", "13", "29–29.5"),
    ("30", "46", "12", "13.5", "29.5–30"),
)
_CHATBOT_CHILD_SIZE_GUIDE = tuple(
    (f"{size / 2:g}", f"{size / 2:g}–{(size + 1) / 2:g}")
    for size in range(24, 48)
)
_CHATBOT_FAQ = {
    "hours": (
        "Nuestro horario de atención es de lunes a viernes, de 9:00 a 18:00, y sábados de 10:00 a 14:00 "
        "(hora del centro de México). Los domingos no hay atención. Puedes escribirnos en cualquier "
        "momento a sneakerpoint@gmail.com y te responderemos dentro del horario de atención."
    ),
    "shipping": (
        "¡Sí, hacemos envíos a todo México! El envío estándar cuesta $99 MXN y es gratis en compras "
        "mayores a $1,500 MXN. El tiempo estimado de entrega es de 3 a 7 días hábiles, según tu ubicación. "
        "Te compartiremos los datos de seguimiento cuando tu pedido salga."
    ),
    "returns": (
        "Aceptamos solicitudes de cambios y devoluciones dentro de los 30 días naturales posteriores a la entrega. "
        "El calzado debe estar sin uso y en su caja original. La solicitud requiere revisión; una vez aprobado "
        "el reembolso, puede tardar de 5 a 10 días hábiles en reflejarse, según tu método de pago. "
        "Escríbenos a sneakerpoint@gmail.com o llama al 55 6127 2829 para iniciar la solicitud."
    ),
    "payment": (
        "En el checkout puedes elegir transferencia bancaria, efectivo al recibir o Mercado Pago. "
        "La opción disponible y las instrucciones se muestran al finalizar tu compra."
    ),
    "contact": "Puedes contactarnos por correo en sneakerpoint@gmail.com o llamar al 55 6127 2829.",
    "location": (
        "No hay una dirección de tienda publicada en la información disponible. "
        "Puedes preguntar por correo en sneakerpoint@gmail.com o llamar al 55 6127 2829."
    ),
    "social": (
        "Facebook, Instagram y X aparecen como próximamente; no hay enlaces oficiales de redes sociales "
        "publicados todavía. Puedes contactarnos por correo en sneakerpoint@gmail.com."
    ),
    "login": (
        "Para comprar, crea una cuenta desde “Regístrate” o inicia sesión. Después agrega los productos al "
        "carrito, completa tus datos de entrega y elige un método de pago disponible."
    ),
    "technical": (
        "Si una página no carga, prueba actualizarla o volver a iniciar sesión. Si el problema continúa, "
        "escríbenos a sneakerpoint@gmail.com o llama al 55 6127 2829."
    ),
    "promotions": "No hay promociones o descuentos vigentes publicados en la información disponible.",
}


def _chatbot_normalize(value):
    normalized = unicodedata.normalize("NFD", value.lower())
    return "".join(character for character in normalized if unicodedata.category(character) != "Mn")


def _chatbot_tokens(value):
    return [
        token for token in re.findall(r"[a-z0-9]+", _chatbot_normalize(value))
        if len(token) > 1 and token not in _CHATBOT_STOP_WORDS
    ]


def _chatbot_classify_intents(message):
    normalized = _chatbot_normalize(message)
    intents = []
    patterns = (
        ("social", r"\b(facebook|face|fb|instagram|insta|redes sociales|redes|seguirlos|pagina oficial|canales oficiales|twitter)\b"),
        ("hours", r"\b(horario|horarios|abren|abierto|atienden|cierran|domingo|domingos)\b"),
        ("shipping", r"\b(envio|envios|entrega|entregas|paquete|paqueteria|rastreo|rastrear|seguimiento|tarda|tardar|tardara|llegar|llegue|yegar)\b"),
        ("returns", r"\b(devolucion|devolver|devoluciones|cambio|cambiar|reembolso|garantia|no me queda|no le queda)\b"),
        ("size", r"\b(guia de tallas?|medir|equivalencia|equivalencias|que talla soy|saber que talla|talla corresponde|medida del pie|largo del pie|pie mide|mido \d|mx\s*\d{2})\b"),
        ("payment", r"\b(pago|pagos|pagar|metodos de pago|transferencia|efectivo|mercado pago|tarjeta|aceptan .{0,25} pagar)\b"),
        ("location", r"\b(ubicacion|direccion|donde se encuentran|donde estan|sucursal|tienda fisica)\b"),
        ("contact", r"\b(contacto|contactar|contactarlos?|telefono|correo|email|escribirles|llamarles)\b"),
        ("account", r"\b(registro|registrarme|registrar|cuenta|iniciar sesion|login|acceso|contrasena)\b"),
        ("technical", r"\b(no carga|no funciona|error|problema tecnico|falla|no puedo entrar|no me deja|no abre)\b"),
        ("promotions", r"\b(promocion|promociones|descuento|descuentos|oferta|ofertas)\b"),
        ("social", r"\b(que redes|cuales redes|red social|pagina de facebook|pagina oficial)\b"),
        ("purchase", r"\b(como (?:puedo )?comprar|como hago una compra)\b"),
        ("clarification", r"\b(no entendi|no entiendo|respuesta anterior|que quisiste decir|no comprendi)\b"),
    )
    for intent, pattern in patterns:
        if re.search(pattern, normalized) and intent not in intents:
            intents.append(intent)

    if re.search(r"\b(mi carrito|carrito de compras|que hay en el carrito|mi compra)\b", normalized):
        intents.append("cart")
    if (
        re.search(r"\b(mis pedidos|mis compras|mi pedido|estado del pedido|seguimiento de mi pedido|rastrear mi pedido)\b", normalized)
        and not re.search(r"\b(cuanto tarda|cuando llega|fecha de entrega)\b", normalized)
    ):
        intents.append("orders")

    has_product_terms = bool(re.search(
        r"\b(tenis|zapatos|calzado|producto|productos|modelo|modelos|catalogo|marca|marcas|"
        r"color|colores|stock|existencia|existencias|disponible|disponibles|disponibilidad|"
        r"nike|adidas|puma|reebok|converse|vans|new balance|jordan|asics|under armour)\b",
        normalized,
    ))
    size_is_product_filter = bool(
        re.search(r"\btalla\s*\d", normalized)
        and not re.search(r"\b(guia|equivalencia|medida|pie)\b", normalized)
    )
    if has_product_terms or size_is_product_filter:
        intents.append("catalog")

    if re.search(r"^\s*(hola|ola|buenas(?:\s+(?:tardes|noches))?|buenos dias)\b", normalized):
        intents.append("greeting")
    if re.search(r"\b(gracias|muchas gracias|te agradezco)\b", normalized):
        intents.append("thanks")
    if re.search(r"\b(adios|hasta luego|nos vemos|bye)\b", normalized):
        intents.append("farewell")
    return intents


def _chatbot_previous_catalog_terms(history):
    context_breakers = {
        "account", "cart", "contact", "hours", "location", "orders", "payment",
        "returns", "shipping", "size", "social", "technical",
    }
    for previous_message in reversed(history[-8:]):
        previous_intents = _chatbot_classify_intents(previous_message)
        if context_breakers.intersection(previous_intents):
            return []
        previous_tokens = [token for token in _chatbot_tokens(previous_message) if not token.isdigit()]
        if previous_tokens:
            return previous_tokens
    return []


def _chatbot_catalog(message, history):
    normalized = _chatbot_normalize(message)
    price_match = re.search(
        r"(?:menos de|menor(?:es)?(?: a| que)?|hasta|maximo(?: de)?)\s*\$?\s*([\d,]+(?:\.\d{1,2})?)",
        normalized,
    )
    max_price = Decimal(price_match.group(1).replace(",", "")) if price_match else None
    size_match = re.search(r"\btalla\s*(?:numero\s*)?(\d{1,2}(?:[.,]\d{1,2})?)\b", normalized)
    requested_size = size_match.group(1).replace(",", ".") if size_match else None
    asking_sizes = bool(re.search(r"\btallas?\b", normalized)) and requested_size is None

    tokens = [token for token in _chatbot_tokens(message) if not token.isdigit()]
    follow_up = bool(re.search(
        r"\b(y|tambien|otra|otro|otras|otros|mas|cuanto|cuesta|precio|disponible|talla|color|"
        r"cuestan|sale|salen|barato|baratos|barata|baratas|economico|economicos|cara|caro|caros|premium|"
        r"menos|menor|hasta|maximo)\b",
        normalized,
    ))
    if follow_up and history:
        previous_tokens = _chatbot_previous_catalog_terms(history)
        tokens = list(dict.fromkeys(previous_tokens + tokens))
    tokens = tokens[:5]

    clauses = ["activo = TRUE"]
    parameters = []
    for token in tokens:
        clauses.append(
            "(nombre ILIKE %s OR marca ILIKE %s OR color ILIKE %s "
            "OR descripcion ILIKE %s OR estilo ILIKE %s)"
        )
        pattern = f"%{token}%"
        parameters.extend([pattern] * 5)
    if requested_size is not None:
        clauses.append("talla = %s")
        parameters.append(requested_size)
    if max_price is not None:
        clauses.append("precio <= %s")
        parameters.append(max_price)

    if asking_sizes:
        if not tokens:
            return "Dime la marca o el modelo para consultar las tallas y existencias actuales."
        sizes = _db_query(
            """SELECT nombre, marca, talla, stock FROM productos
               WHERE """ + " AND ".join(clauses) + " ORDER BY nombre, talla LIMIT 20",
            tuple(parameters), fetch_all=True,
        )
        if not sizes:
            return "No encontré tallas registradas para ese modelo en el catálogo activo."
        lines = []
        for product in sizes:
            stock = f"{product['stock']} pares" if product["stock"] else "agotada"
            lines.append(
                f"• {product['nombre']} ({product['marca']}) — "
                f"talla {product['talla']}: {stock}."
            )
        return "Tallas y existencias actuales del catálogo:\n" + "\n".join(lines)

    ordering = "precio ASC" if any(word in normalized for word in ("barato", "baratos", "economico", "economicos")) else "precio DESC" if any(
        word in normalized for word in ("caro", "caros", "premium")
    ) else "nombre"

    query = (
        "SELECT id_producto, nombre, marca, precio, stock, color, talla "
        f"FROM productos WHERE {' AND '.join(clauses)} ORDER BY {ordering} LIMIT 5"
    )
    products = _db_query(query, tuple(parameters), fetch_all=True)
    if not products:
        if tokens or max_price is not None or requested_size is not None:
            return "No encontré modelos activos que coincidan con esa búsqueda. Puedes probar con otra marca, color o modelo."
        return "Dime qué marca, modelo o estilo buscas y consultaré el catálogo disponible."

    lines = []
    for product in products:
        stock = f"{product['stock']} pares disponibles" if product["stock"] else "agotado por el momento"
        details = [product["marca"]]
        if product.get("color"):
            details.append(product["color"])
        if product.get("talla"):
            details.append(f"talla {product['talla']}")
        lines.append(
            f"• {product['nombre']} ({' · '.join(details)}) — "
            f"${product['precio']:,.2f} MXN; {stock}."
        )
    answer = "Estos son algunos modelos del catálogo:\n" + "\n".join(lines)
    if len(products) == 5:
        answer += "\nPuedes afinar la búsqueda por marca, modelo o color."
    return answer


def _chatbot_size_response(message):
    normalized = _chatbot_normalize(message)
    measurement_match = re.search(r"\b(\d{1,2}(?:[.,]\d{1,2})?)\s*(?:cm|centimetros?)\b", normalized)
    if measurement_match:
        measurement = float(measurement_match.group(1).replace(",", "."))
        guide_rows = _CHATBOT_ADULT_SIZE_GUIDE
        if measurement < 23.5:
            guide_rows = _CHATBOT_CHILD_SIZE_GUIDE
        matches = []
        for row in guide_rows:
            lower, upper = (float(value.replace(",", ".")) for value in row[-1].split("–"))
            if lower <= measurement <= upper:
                matches.append(row)
        if not matches:
            return "Esa medida está fuera de los rangos de la guía publicada. Consulta con la tienda para confirmar la talla adecuada."
        if guide_rows is _CHATBOT_CHILD_SIZE_GUIDE:
            sizes = ", ".join(f"MX infantil {row[0]}" for row in matches)
        else:
            sizes = "; ".join(
                f"MX {row[0]} (EU {row[1]}, US hombre {row[2]}, US mujer {row[3]})"
                for row in matches
            )
        return (
            f"Para un pie de {measurement:g} cm, la guía muestra {sizes}. "
            "Es una equivalencia orientativa; el ajuste puede variar según la marca y el modelo."
        )

    conversion_match = re.search(r"\bmx\s*(\d{2}(?:[.,]5)?)\b", normalized)
    if conversion_match:
        requested_size = conversion_match.group(1).replace(",", ".")
        row = next((row for row in _CHATBOT_ADULT_SIZE_GUIDE if row[0] == requested_size), None)
        if row:
            return (
                f"En la guía, MX {row[0]} equivale aproximadamente a EU {row[1]}, "
                f"US hombre {row[2]} o US mujer {row[3]}; el largo de pie indicado es {row[4]} cm."
            )

    return (
        "La guía de la tienda recomienda medir ambos pies de pie, del talón a la punta del dedo más largo, "
        "y usar la medida del pie mayor. Las equivalencias son aproximadas y pueden variar por marca y modelo. "
        "Dime la longitud en centímetros o la talla MX que quieres convertir."
    )


def _chatbot_faq_response(intent, message):
    normalized = _chatbot_normalize(message)
    if intent == "hours":
        return _CHATBOT_FAQ["hours"]
    if intent == "shipping":
        if any(word in normalized for word in ("rastrear", "rastreo", "seguimiento")):
            return _CHATBOT_FAQ["shipping"] + " El número de seguimiento se comparte cuando el pedido sale."
        return _CHATBOT_FAQ["shipping"]
    if intent == "returns":
        return _CHATBOT_FAQ["returns"]
    if intent == "payment":
        return _CHATBOT_FAQ["payment"]
    if intent == "social":
        return _CHATBOT_FAQ["social"]
    if intent == "location":
        return _CHATBOT_FAQ["location"]
    if intent == "contact":
        return _CHATBOT_FAQ["contact"]
    if intent == "account":
        return _CHATBOT_FAQ["login"]
    if intent == "technical":
        return _CHATBOT_FAQ["technical"]
    if intent == "size":
        return _chatbot_size_response(message)
    if intent == "promotions":
        return _CHATBOT_FAQ["promotions"]
    if intent == "purchase":
        return (
            "Elige un modelo y su talla, agrégalo al carrito, inicia sesión o crea una cuenta, "
            "completa tus datos de entrega y selecciona uno de los métodos de pago disponibles en el checkout."
        )
    return ""


def _chatbot_provider_response(message, history):
    normalized = _chatbot_normalize(message)
    intents = _chatbot_classify_intents(message)

    if any(intent in intents for intent in ("cart", "orders")) or re.search(
        r"\b(pedido|pedidos|cliente|clientes|carrito|ventas)\b", normalized
    ):
        return "No tengo acceso a pedidos, carritos ni información de clientes. Puedo consultar el catálogo público y el estado de tus propios productos."

    products_intent = bool(re.search(
        r"\b(mis|mi)\b.*\b(productos?|propuestas?|publicaciones?)\b"
        r"|\b(estado|revision|revisar)\b.*\b(productos?|propuestas?|publicaciones?)\b",
        normalized,
    ))
    responses = [
        _chatbot_faq_response(intent, message)
        for intent in intents
        if intent in {"account", "contact", "hours", "location", "payment", "promotions", "purchase", "returns", "shipping", "size", "social", "technical"}
    ]
    if products_intent:
        products = _db_query(
            """SELECT nombre, marca, estado FROM productos_proveedor
               WHERE id_proveedor=%s ORDER BY fecha_creacion DESC LIMIT 8""",
            (session["id_proveedor"],), fetch_all=True,
        )
        if not products:
            responses.append("Aún no has enviado productos. Puedes proponer un modelo desde tu portal de proveedor.")
        else:
            responses.append(
                "Estado de tus productos:\n" + "\n".join(
                    f"• {product['nombre']} ({product['marca']}): {product['estado']}."
                    for product in products
                )
            )
    elif "catalog" in intents:
        responses.append(_chatbot_catalog(message, history))
    elif not responses and intents == ["greeting"]:
        name = session.get("nombre_proveedor")
        greeting = f"¡Hola, {name}! " if name else "¡Hola! "
        responses.append(greeting + "Este asistente consulta el catálogo público y el estado de tus propios productos.")
    elif not responses and intents == ["thanks"]:
        responses.append("¡Con gusto! Si necesitas otra consulta sobre el catálogo o tus productos, aquí estoy.")
    elif not responses and intents == ["farewell"]:
        responses.append("¡Hasta luego! Estoy aquí si necesitas ayuda con tus productos.")
    elif not responses:
        responses.append("Puedo consultar el catálogo público y el estado de tus propios productos. No tengo acceso a pedidos de clientes ni a datos de otros proveedores.")
    return "\n\n".join(responses)


def _chatbot_role_response(message, role, history):
    normalized = _chatbot_normalize(message)
    if role == "provider":
        return _chatbot_provider_response(message, history)

    if role == "admin" and any(word in normalized for word in ("resumen", "estadistica", "ventas", "pendiente", "dashboard")):
        summary = {
            "products": _db_query("SELECT COUNT(*) AS value FROM productos WHERE activo=TRUE", fetch_one=True)["value"],
            "providers": _db_query("SELECT COUNT(*) AS value FROM proveedores", fetch_one=True)["value"],
            "pending": _db_query("SELECT COUNT(*) AS value FROM productos_proveedor WHERE estado='pendiente'", fetch_one=True)["value"],
            "sales": _db_query("SELECT COUNT(*) AS value FROM pedidos WHERE estado <> 'cancelado'", fetch_one=True)["value"],
        }
        return (
            "Resumen administrativo actual:\n"
            f"• Modelos activos: {summary['products']}\n"
            f"• Proveedores registrados: {summary['providers']}\n"
            f"• Propuestas pendientes: {summary['pending']}\n"
            f"• Pedidos no cancelados: {summary['sales']}"
        )
    if role == "admin" and any(word in normalized for word in ("proveedores", "directorio", "contacto del proveedor")):
        providers = _db_query(
            """SELECT razon_social, contacto, correo, telefono, localidad
               FROM proveedores ORDER BY razon_social LIMIT 8""",
            fetch_all=True,
        )
        if not providers:
            return "No hay proveedores registrados en el directorio por el momento."
        entries = []
        for provider in providers:
            details = [
                provider["razon_social"],
                provider.get("contacto") or "Contacto no indicado",
                provider.get("correo") or "Sin correo",
            ]
            if provider.get("telefono"):
                details.append(provider["telefono"])
            entries.append("• " + " · ".join(details))
        return "Directorio interno de proveedores:\n" + "\n".join(entries)

    intents = _chatbot_classify_intents(message)
    responses = [
        _chatbot_faq_response(intent, message)
        for intent in intents
        if intent in {"account", "contact", "hours", "location", "payment", "promotions", "purchase", "returns", "shipping", "size", "social", "technical"}
    ]

    if "cart" in intents:
        if role != "customer":
            responses.append("Inicia sesión como cliente para consultar tu carrito.")
        else:
            items, total = _cart_details()
            if not items:
                responses.append("Tu carrito está vacío. Puedes agregar tenis desde el catálogo.")
            else:
                contents = "\n".join(
                    f"• {item['producto']['nombre']} · talla {item['talla_seleccionada']} × {item['cantidad']}."
                    for item in items
                )
                responses.append(f"Esto tienes en tu carrito:\n{contents}\nTotal: ${total:,.2f} MXN.")

    if "orders" in intents:
        if role != "customer":
            responses.append("Inicia sesión como cliente para consultar el estado de tus pedidos.")
        else:
            orders = _db_query(
                """SELECT id_pedido, estado, total, fecha_creacion FROM pedidos
                   WHERE id_clientes=%s ORDER BY fecha_creacion DESC LIMIT 5""",
                (session["id_cliente"],), fetch_all=True,
            )
            if not orders:
                responses.append("Todavía no tienes pedidos registrados. Cuando confirmes una compra podrás consultarla aquí.")
            else:
                responses.append(
                    "Tus pedidos más recientes:\n" + "\n".join(
                        f"• Pedido #{order['id_pedido']}: {order['estado']} — ${order['total']:,.2f} MXN."
                        for order in orders
                    )
                )

    catalog_follow_up = bool(
        _chatbot_previous_catalog_terms(history)
        and re.search(
            r"\b(y|tambien|otra|otro|otras|otros|mas|cuanto|cuesta|cuestan|precio|precios|"
            r"disponible|talla|color|barato|barata|baratos|baratas|economico|economica|"
            r"economicos|economicas|menor|maximo|maxima|menos)\b",
            normalized,
        )
        and not any(intent != "catalog" for intent in intents)
    )
    if "catalog" in intents or catalog_follow_up:
        if not _chatbot_tokens(message) and re.search(r"\b(cuanto cuesta|cuanto cuestan|precio|precios)\b", normalized) and not catalog_follow_up:
            responses.append("¿Qué marca o modelo quieres consultar? Buscaré el precio actual en el catálogo.")
        else:
            responses.append(_chatbot_catalog(message, history))

    if "greeting" in intents:
        name = session.get("nombre_cliente")
        greeting = f"¡Hola, {name}! " if name else "¡Hola! "
        responses.append(greeting + "Puedo ayudarte con el catálogo, la compra y la información de la tienda.")
    if "clarification" in intents:
        responses.append("Perdón por la confusión. ¿Qué tema querías consultar: un producto, tallas, envíos, devoluciones, pagos u horarios?")
    if "thanks" in intents:
        responses.append("¡Con gusto! Si tienes otra pregunta sobre la tienda, aquí estoy.")
    if "farewell" in intents:
        responses.append("¡Hasta luego! Gracias por visitar Sneaker Point.")

    if role == "admin" and any(intent in intents for intent in ("contact",)):
        responses.append("El directorio de proveedores es una consulta exclusiva del panel de administración.")

    if not responses:
        if (
            not message.strip()
            or re.fullmatch(r"[\W_]+", message, flags=re.UNICODE)
            or re.fullmatch(r"[a-z]{8,}", normalized)
        ):
            return "No alcancé a entender tu mensaje. ¿Puedes reformularlo?"
        if "clarification" in intents:
            return "Perdón por la confusión. ¿Qué tema querías consultar: un producto, tallas, envíos, devoluciones, pagos u horarios?"
        if re.search(r"\b(cuanto cuesta|cuanto cuestan|precio|precios)\b", normalized):
            return "¿Qué marca o modelo quieres consultar? Buscaré el precio actual en el catálogo."
        return (
            "Soy el asistente de Sneaker Point y puedo ayudarte con productos, tallas, compras, pagos, "
            "envíos, devoluciones, horarios y contacto. ¿Qué necesitas consultar?"
        )
    return "\n\n".join(dict.fromkeys(response for response in responses if response))


@app.route("/api/chatbot", methods=["POST"])
def chatbot_message():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="Envía un mensaje en formato JSON."), 400

    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        return jsonify(error="Escribe una pregunta para continuar."), 400
    message = message.strip()
    if len(message) > 500:
        return jsonify(error="El mensaje debe tener 500 caracteres o menos."), 400

    history = payload.get("history", [])
    if not isinstance(history, list) or any(not isinstance(item, str) for item in history):
        return jsonify(error="El contexto de conversación no es válido."), 400
    history = [item.strip()[:500] for item in history[-8:] if item.strip()]

    role = session.get("role", "guest") if session.get("loggedin") else "guest"
    if role not in {"guest", "customer", "provider", "admin"}:
        role = "guest"

    if role == "provider":
        try:
            account = _db_query(
                "SELECT activo FROM proveedores WHERE id_proveedor=%s",
                (session.get("id_proveedor"),), fetch_one=True,
            )
        except Exception:
            logging.exception("Chatbot could not verify provider access")
            return jsonify(error="No puedo verificar el acceso en este momento. Inténtalo de nuevo."), 503
        if not account or not account["activo"]:
            session.clear()
            return jsonify(error="Tu acceso de proveedor está inactivo. Inicia sesión o contacta al equipo administrador."), 403

    try:
        reply = _chatbot_role_response(message, role, history)
    except Exception:
        logging.exception("Chatbot request failed")
        return jsonify(error="No pude consultar la información en este momento. Inténtalo de nuevo en unos minutos."), 503
    return jsonify(reply=reply, role=role)


@app.route("/add_cart", methods=["POST"])
def add_cart():
    try:
        product_id = int(request.form.get("id_producto", ""))
        quantity = int(request.form.get("cantidad", "1"))
        size = request.form.get("talla", "").strip()
        if quantity < 1 or not size:
            raise ValueError
        product = _db_query(
            "SELECT id_producto, nombre, descripcion, estilo, stock FROM productos WHERE id_producto = %s AND activo = TRUE",
            (product_id,), fetch_one=True,
        )
        if not product:
            flash("Este modelo ya no está disponible.", "error")
            return redirect(url_for("index"))
        if size not in _shoe_size_options(_is_child_shoe(product)):
            raise ValueError
        cart_name = "cart" if session.get("loggedin") and session.get("role") == "customer" else "pending_items"
        cart = session.get(cart_name, {})
        existing_quantity = sum(
            int(cart_quantity)
            for cart_key, cart_quantity in cart.items()
            if cart_key.split("_", 1)[0] == str(product_id)
        )
        if existing_quantity + quantity > product["stock"]:
            flash(f"No hay existencias suficientes de {product['nombre']}.", "error")
            return redirect(url_for("index"))
    except (TypeError, ValueError):
        flash("Selecciona una talla y cantidad válidas.", "error")
        return redirect(url_for("index"))
    except Exception:
        flash("No pudimos agregar el modelo al carrito.", "error")
        return redirect(url_for("index"))
    if not session.get("loggedin") or session.get("role") != "customer":
        pending_items = session.get("pending_items", {})
        key = f"{product_id}_{size}"
        pending_items[key] = int(pending_items.get(key, 0)) + quantity
        session["pending_items"] = pending_items
        return redirect(url_for("login", next=url_for("carrito")))
    cart = session.setdefault("cart", {})
    key = f"{product_id}_{size}"
    cart[key] = int(cart.get(key, 0)) + quantity
    session.modified = True
    flash("Tenis agregado al carrito.", "success")
    return redirect(url_for("index"))


@app.route("/carrito")
@customer_required
def carrito():
    try:
        items, total = _cart_details()
    except Exception:
        items, total = [], 0
        flash("No fue posible cargar el carrito.", "error")
    return render_template("carrito.html", items=items, total=total)


@app.route("/eliminar/<path:cart_key>", methods=["POST"])
@customer_required
def eliminar(cart_key):
    session.get("cart", {}).pop(cart_key, None)
    session.modified = True
    return redirect(url_for("carrito"))


@app.route("/comprar", methods=["GET", "POST"])
@customer_required
def comprar():
    try:
        items, subtotal = _cart_details()
    except Exception:
        items, subtotal = [], 0
    if not items:
        flash("Tu carrito está vacío.", "warning")
        return redirect(url_for("index"))
    if request.method == "GET":
        try:
            customer = _db_query(
                "SELECT nombre, apellido, correo, telefono FROM clientes WHERE id_clientes=%s",
                (session["id_cliente"],), fetch_one=True,
            )
        except Exception:
            customer = None
        return render_template("compra.html", items=items, customer=customer, total=subtotal)

    fields = {
        "nombre": request.form.get("nombre", "").strip(),
        "apellido": request.form.get("apellido", "").strip(),
        "correo": request.form.get("correo", "").strip(),
        "telefono": request.form.get("telefono", "").strip(),
        "calle": request.form.get("calle", "").strip(),
        "numero_exterior": request.form.get("num_ext", "").strip(),
        "numero_interior": request.form.get("num_int", "").strip(),
        "codigo_postal": request.form.get("CP", "").strip(),
        "metodo_pago": request.form.get("metodo_pago", "").strip(),
    }
    if not all([fields["nombre"], fields["apellido"], fields["correo"], fields["telefono"], fields["calle"], fields["numero_exterior"], fields["codigo_postal"]]) or fields["metodo_pago"] not in {"Transferencia", "Efectivo"}:
        flash("Completa los datos de entrega y selecciona un método de pago.", "error")
        return render_template("compra.html", items=items, total=subtotal)

    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = _cursor(conn)
        total = 0
        locked_items = _locked_cart_items(cur)
        for product, quantity, size in locked_items:
            total += product["precio"] * quantity

        _execute(cur,
                """INSERT INTO pedidos
                    (id_clientes, nombre, apellido, correo, telefono, calle, numero_exterior,
                     numero_interior, codigo_postal, metodo_pago, total, estado)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pendiente')
                    RETURNING id_pedido""",
                (session["id_cliente"], fields["nombre"], fields["apellido"], fields["correo"],
                 fields["telefono"], fields["calle"], fields["numero_exterior"],
                 fields["numero_interior"], fields["codigo_postal"], fields["metodo_pago"], total),
        )
        order_id = _fetchone(cur)["id_pedido"]
        for product, quantity, size in locked_items:
            line_total = product["precio"] * quantity
            _execute(cur,
                """INSERT INTO ventas (id_producto, id_clientes, id_pedido, cantidad, total, talla, metodo_pago)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (product["id_producto"], session["id_cliente"], order_id, quantity, line_total, size, fields["metodo_pago"]),
            )
            _execute(cur, "UPDATE productos SET stock = stock - %s WHERE id_producto = %s", (quantity, product["id_producto"]))
        conn.commit()
    except (ValueError,) + _database_error_types() as error:
        if conn:
            conn.rollback()
        logging.exception("Order checkout failed")
        flash(str(error) if isinstance(error, ValueError) else "No se pudo completar el pedido. Intenta de nuevo.", "error")
        return redirect(url_for("carrito"))
    except Exception:
        if conn:
            conn.rollback()
        logging.exception("Unexpected checkout error")
        flash("Ocurrió un error al completar el pedido.", "error")
        return redirect(url_for("carrito"))
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    session.pop("cart", None)
    flash(f"Pedido #{order_id} recibido. Gracias por tu compra.", "success")
    return redirect(url_for("mis_pedidos"))


def _mp_preference_data(items, total, order_id=None):
    preference_items = []
    for item in items:
        preference_items.append({
            "title": item["producto"]["nombre"],
            "quantity": int(item["cantidad"]),
            "unit_price": float(item["producto"]["precio"]),
            "currency_id": "MXN",
        })
    preference_data = {
        "items": preference_items,
        "external_reference": str(order_id) if order_id else None,
    }
    if MP_BASE_URL:
        preference_data["back_urls"] = {
            "success": f"{MP_BASE_URL}{url_for('pago_mp_exito')}",
            "failure": f"{MP_BASE_URL}{url_for('pago_mp_fallo')}",
            "pending": f"{MP_BASE_URL}{url_for('pago_mp_pendiente')}",
        }
        preference_data["auto_return"] = "approved"
    return preference_data


def _mp_checkout_url(preference):
    if not isinstance(preference, dict):
        return None
    if MP_ACCESS_TOKEN.startswith("TEST-"):
        return preference.get("sandbox_init_point")
    return preference.get("init_point")


@app.route("/pago/mercadopago/crear", methods=["POST"])
@customer_required
def pago_mp_crear():
    if not mp_sdk:
        return jsonify({"error": "Mercado Pago no está configurado. Agrega MERCADOPAGO_ACCESS_TOKEN a .env."}), 503

    try:
        items, total = _cart_details()
    except Exception:
        logging.exception("Could not load cart for Mercado Pago checkout")
        return jsonify({"error": "No se pudo cargar tu carrito. Intenta de nuevo."}), 500
    if not items:
        return jsonify({"error": "Tu carrito está vacío. Agrega productos antes de pagar."}), 400

    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = _cursor(conn)
        total = 0
        locked_items = _locked_cart_items(cur)
        for product, quantity, size in locked_items:
            total += product["precio"] * quantity

        fields = {
            "nombre": request.form.get("nombre", "").strip(),
            "apellido": request.form.get("apellido", "").strip(),
            "correo": request.form.get("correo", "").strip(),
            "telefono": request.form.get("telefono", "").strip(),
            "calle": request.form.get("calle", "").strip(),
            "numero_exterior": request.form.get("num_ext", "").strip(),
            "numero_interior": request.form.get("num_int", "").strip(),
            "codigo_postal": request.form.get("CP", "").strip(),
        }
        if not all([fields["nombre"], fields["apellido"], fields["correo"], fields["telefono"], fields["calle"], fields["numero_exterior"], fields["codigo_postal"]]):
            raise ValueError("Completa todos los datos de entrega.")

        _execute(cur,
                """INSERT INTO pedidos
                    (id_clientes, nombre, apellido, correo, telefono, calle, numero_exterior,
                     numero_interior, codigo_postal, metodo_pago, total, estado)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pendiente')
                    RETURNING id_pedido""",
                (session["id_cliente"], fields["nombre"], fields["apellido"], fields["correo"],
                 fields["telefono"], fields["calle"], fields["numero_exterior"],
                 fields["numero_interior"], fields["codigo_postal"], "Mercado Pago", total),
        )
        order_id = _fetchone(cur)["id_pedido"]
        for product, quantity, size in locked_items:
            line_total = product["precio"] * quantity
            _execute(cur,
                """INSERT INTO ventas (id_producto, id_clientes, id_pedido, cantidad, total, talla, metodo_pago)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (product["id_producto"], session["id_cliente"], order_id, quantity, line_total, size, "Mercado Pago"),
            )
            _execute(cur, "UPDATE productos SET stock = stock - %s WHERE id_producto = %s", (quantity, product["id_producto"]))
        conn.commit()
    except ValueError as error:
        if conn:
            conn.rollback()
        logging.exception("Order checkout failed")
        return jsonify({"error": str(error)}), 400
    except _database_error_types():
        if conn:
            conn.rollback()
        logging.exception("Order checkout failed")
        return jsonify({"error": "No se pudo completar el pedido. Revisa la conexión con la base de datos y vuelve a intentar."}), 500
    except Exception:
        if conn:
            conn.rollback()
        logging.exception("Unexpected checkout error")
        return jsonify({"error": "Ocurrió un error al preparar el pedido. Intenta de nuevo."}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    preference_data = _mp_preference_data(items, total, order_id)
    try:
        preference_response = mp_sdk.preference().create(preference_data)
    except Exception:
        logging.exception("Could not create Mercado Pago preference")
        _update_order_payment_status(order_id, None, "rejected")
        return jsonify({"error": "No se pudo conectar con Mercado Pago. Verifica el token y vuelve a intentarlo."}), 502

    if not isinstance(preference_response, dict):
        logging.error("Mercado Pago returned an invalid preference response")
        _update_order_payment_status(order_id, None, "rejected")
        return jsonify({"error": "Mercado Pago respondió con un formato inesperado. Vuelve a intentarlo."}), 502

    preference = preference_response.get("response")
    checkout_url = _mp_checkout_url(preference)
    preference_id = preference.get("id") if isinstance(preference, dict) else None
    if not checkout_url or not preference_id:
        causes = preference.get("cause", []) if isinstance(preference, dict) else []
        details = "; ".join(
            cause["description"] for cause in causes
            if isinstance(cause, dict) and cause.get("description")
        )
        detail = details or (preference.get("message") if isinstance(preference, dict) else None)
        logging.error(
            "Mercado Pago rejected preference creation with status %s: %s",
            preference_response.get("status"), detail or "sin detalle",
        )
        message = "Mercado Pago rechazó la solicitud."
        if detail:
            message = f"{message} {detail}"
        _update_order_payment_status(order_id, None, "rejected")
        return jsonify({"error": message}), 502

    return jsonify({
        "init_point": checkout_url,
        "preference_id": preference_id,
        "order_id": order_id,
    })


def _verify_mp_payment(payment_id):
    if not mp_sdk or not payment_id:
        return None
    try:
        payment_response = mp_sdk.payment().get(payment_id)
        return payment_response["response"]
    except Exception:
        logging.exception("Could not verify Mercado Pago payment")
        return None


def _update_order_payment_status(order_id, payment_id, status, customer_id=None, amount=None, currency=None):
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = _cursor(conn)
        if customer_id is None:
            _execute(cur, "SELECT id_pedido, estado, total FROM pedidos WHERE id_pedido = %s FOR UPDATE", (order_id,))
        else:
            _execute(cur, "SELECT id_pedido, estado, total FROM pedidos WHERE id_pedido = %s AND id_clientes = %s FOR UPDATE", (order_id, customer_id))
        order = _fetchone(cur)
        if not order:
            return False, "Pedido no encontrado"
        if order["estado"] == "pagado":
            return True, "El pedido ya fue pagado"
        if status == "approved":
            try:
                payment_total = Decimal(str(amount))
            except (InvalidOperation, TypeError, ValueError):
                return False, "Importe de pago inválido"
            if currency != "MXN" or payment_total != Decimal(str(order["total"])):
                return False, "El importe o la moneda del pago no coinciden con el pedido"
            if order["estado"] == "cancelado":
                return False, "El pedido está cancelado"
            _execute(cur, "UPDATE pedidos SET estado = 'pagado' WHERE id_pedido = %s", (order_id,))
            _execute(cur, "UPDATE ventas SET referencia_pago = %s WHERE id_pedido = %s", (str(payment_id), order_id))
            conn.commit()
            if customer_id is not None:
                session.pop("cart", None)
            return True, "Pago confirmado"
        if status in {"pending", "in_process"} and order["estado"] != "cancelado":
            conn.commit()
            return True, "Pago pendiente"
        if status in {"rejected", "cancelled"} and order["estado"] != "cancelado":
            _restore_order_stock(cur, order_id)
            _execute(cur, "UPDATE pedidos SET estado = 'cancelado' WHERE id_pedido = %s", (order_id,))
        conn.commit()
        return True, "Estado de pago procesado"
    except Exception:
        if conn:
            conn.rollback()
        logging.exception("Could not update payment status")
        return False, "Error al actualizar el pedido"
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/pago/mercadopago/exito")
@customer_required
def pago_mp_exito():
    payment_id = request.args.get("payment_id")
    order_id = request.args.get("external_reference")
    payment = _verify_mp_payment(payment_id)
    if payment and payment.get("status") == "approved" and str(payment.get("external_reference")) == str(order_id):
        success, message = _update_order_payment_status(
            order_id,
            payment_id,
            "approved",
            customer_id=session["id_cliente"],
            amount=payment.get("transaction_amount"),
            currency=payment.get("currency_id"),
        )
        if success:
            flash(f"Pago confirmado. Pedido #{order_id} pagado.", "success")
        else:
            flash(f"Pago recibido pero hubo un problema: {message}", "warning")
    else:
        flash("No se pudo verificar el pago. Contacta al administrador.", "warning")
    return redirect(url_for("mis_pedidos"))


@app.route("/pago/mercadopago/fallo")
@customer_required
def pago_mp_fallo():
    order_id = request.args.get("external_reference")
    if order_id:
        _update_order_payment_status(order_id, None, "rejected", customer_id=session["id_cliente"])
    flash("El pago fue rechazado. Puedes intentar de nuevo.", "error")
    return redirect(url_for("carrito"))


@app.route("/pago/mercadopago/pendiente")
@customer_required
def pago_mp_pendiente():
    order_id = request.args.get("external_reference")
    if order_id:
        _update_order_payment_status(order_id, None, "pending", customer_id=session["id_cliente"])
    flash("El pago está pendiente. Te notificaremos cuando se confirme.", "info")
    return redirect(url_for("mis_pedidos"))


@app.route("/pago/mercadopago/webhook", methods=["POST"])
def pago_mp_webhook():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    if not MP_WEBHOOK_SECRET:
        logging.error("Mercado Pago webhook secret is not configured")
        return jsonify({"error": "Webhook no configurado"}), 503

    body_data_id = str((data.get("data") or {}).get("id", ""))
    query_data_id = request.args.get("data.id", "")
    if body_data_id and query_data_id and body_data_id.lower() != query_data_id.lower():
        return jsonify({"error": "Payment ID mismatch"}), 400
    payment_id = query_data_id or body_data_id
    if not payment_id or not _valid_mp_webhook_signature(
        request.headers.get("x-signature", ""),
        request.headers.get("x-request-id", ""),
        payment_id,
    ):
        return jsonify({"error": "Unauthorized"}), 401

    action = data.get("action", "")
    if action not in ("payment.created", "payment.updated"):
        return jsonify({"status": "ignored"}), 200

    payment = _verify_mp_payment(payment_id)
    if not payment:
        return jsonify({"error": "Payment not found"}), 404
    if payment.get("id") is not None and str(payment["id"]) != str(payment_id):
        return jsonify({"error": "Payment ID mismatch"}), 400

    external_reference = payment.get("external_reference")
    if not external_reference or not str(external_reference).isdigit():
        return jsonify({"error": "Missing external reference"}), 400

    order_id = int(external_reference)
    status = payment.get("status")
    success, message = _update_order_payment_status(
        order_id,
        payment_id,
        status,
        amount=payment.get("transaction_amount"),
        currency=payment.get("currency_id"),
    )
    if not success:
        return jsonify({"error": message}), 400
    return jsonify({"status": "processed"}), 200


def _valid_mp_webhook_signature(signature, request_id, data_id):
    if not MP_WEBHOOK_SECRET or not signature or not request_id or not data_id:
        return False

    signature_parts = {}
    for part in signature.split(","):
        key, separator, value = part.strip().partition("=")
        if separator:
            signature_parts[key] = value

    timestamp = signature_parts.get("ts")
    provided_hash = signature_parts.get("v1")
    if not timestamp or not provided_hash:
        return False

    manifest = f"id:{str(data_id).lower()};request-id:{request_id};ts:{timestamp};"
    expected_hash = hmac.new(
        MP_WEBHOOK_SECRET.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_hash, provided_hash)


@app.route("/mis-pedidos")
@customer_required
def mis_pedidos():
    try:
        pedidos = _db_query(
            """SELECT id_pedido, total, estado, metodo_pago, fecha_creacion FROM pedidos
               WHERE id_clientes = %s ORDER BY fecha_creacion DESC""",
            (session["id_cliente"],), fetch_all=True,
        )
        for pedido in pedidos:
            pedido["productos"] = _db_query(
                """SELECT p.nombre, p.marca, v.cantidad, v.talla, v.total FROM ventas v
                   JOIN productos p ON p.id_producto = v.id_producto WHERE v.id_pedido = %s""",
                (pedido["id_pedido"],), fetch_all=True,
            )
    except Exception:
        pedidos = []
    return render_template("mis_pedidos.html", pedidos=pedidos)


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    try:
        summary = {
            "products": _db_query("SELECT COUNT(*) AS value FROM productos WHERE activo=TRUE", fetch_one=True)["value"],
            "providers": _db_query("SELECT COUNT(*) AS value FROM proveedores", fetch_one=True)["value"],
            "active_providers": _db_query("SELECT COUNT(*) AS value FROM proveedores WHERE activo=TRUE", fetch_one=True)["value"],
            "pending": _db_query("SELECT COUNT(*) AS value FROM productos_proveedor WHERE estado = 'pendiente'", fetch_one=True)["value"],
            "sales": _db_query("SELECT COALESCE(SUM(total), 0) AS value FROM pedidos WHERE estado <> 'cancelado'", fetch_one=True)["value"],
        }
        proposals = _db_query(
            """SELECT pp.*, pr.razon_social FROM productos_proveedor pp
               JOIN proveedores pr ON pr.id_proveedor = pp.id_proveedor
               WHERE pp.estado = 'pendiente' ORDER BY pp.fecha_creacion DESC""",
            fetch_all=True,
        )
        recent_orders = _db_query(
                """SELECT pe.id_pedido, pe.total, pe.estado, pe.fecha_creacion, pe.nombre, pe.apellido
                    FROM pedidos pe
               ORDER BY pe.fecha_creacion DESC LIMIT 8""", fetch_all=True,
        )
        providers = _db_query(
            """SELECT pr.id_proveedor, pr.razon_social, pr.contacto, pr.correo,
                      pr.telefono, pr.localidad, pr.activo,
                      STRING_AGG(pp.nombre, ', ' ORDER BY pp.nombre) AS modelos
               FROM proveedores pr
               LEFT JOIN (SELECT DISTINCT id_proveedor, nombre FROM productos_proveedor) pp
                   ON pp.id_proveedor=pr.id_proveedor
               GROUP BY pr.id_proveedor, pr.razon_social, pr.contacto, pr.correo,
                        pr.telefono, pr.localidad, pr.activo
               ORDER BY pr.razon_social LIMIT 8""",
            fetch_all=True,
        )
    except Exception:
        summary = {"products": 0, "providers": 0, "active_providers": 0, "pending": 0, "sales": 0}
        proposals, recent_orders, providers = [], [], []
    return render_template("admin_dashboard.html", summary=summary, proposals=proposals, recent_orders=recent_orders, providers=providers)


@app.route("/admin/productos")
@admin_required
def admin_productos():
    try:
        productos = _db_query("SELECT * FROM productos WHERE activo=TRUE ORDER BY nombre", fetch_all=True)
    except Exception:
        productos = []
    return render_template("admin_productos.html", productos=productos, estilos=PRODUCT_STYLES)


@app.route("/admin/productos/nuevo", methods=["POST"])
@admin_required
def admin_producto_nuevo():
    values = _product_form_values()
    if not values:
        return redirect(url_for("admin_productos"))
    try:
        _db_query(
            """INSERT INTO productos (nombre, marca, estilo, descripcion, precio, stock, color, talla, imagen_url, activo)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)""", values, commit=True,
        )
        flash("Producto publicado en el catálogo.", "success")
    except Exception:
        flash("No se pudo crear el producto.", "error")
    return redirect(url_for("admin_productos"))


def _product_form_values():
    nombre = request.form.get("nombre", "").strip()
    marca = request.form.get("marca", "").strip()
    estilo = request.form.get("estilo", "").strip()
    descripcion = request.form.get("descripcion", "").strip()
    color = request.form.get("color", "").strip()
    talla = request.form.get("talla", "").strip()
    imagen_url = request.form.get("imagen_url", "").strip()
    try:
        precio = float(request.form.get("precio", ""))
        stock = int(request.form.get("stock", ""))
        if (
            not nombre
            or not marca
            or estilo not in PRODUCT_STYLES
            or not math.isfinite(precio)
            or precio < 0
            or stock < 0
        ):
            raise ValueError
        return (nombre, marca, estilo, descripcion, precio, stock, color, talla, imagen_url)
    except (TypeError, ValueError):
        flash("Verifica nombre, marca, estilo, precio y existencias.", "error")
        return None


@app.route("/admin/productos/<int:product_id>/editar", methods=["POST"])
@admin_required
def admin_producto_editar(product_id):
    values = _product_form_values()
    if values:
        try:
            _db_query(
                """UPDATE productos SET nombre=%s, marca=%s, estilo=%s, descripcion=%s, precio=%s, stock=%s,
                   color=%s, talla=%s, imagen_url=%s WHERE id_producto=%s""",
                values + (product_id,), commit=True,
            )
            flash("Producto actualizado.", "success")
        except Exception:
            flash("No se pudo actualizar el producto.", "error")
    return redirect(url_for("admin_productos"))


@app.route("/admin/productos/<int:product_id>/eliminar", methods=["POST"])
@admin_required
def admin_producto_eliminar(product_id):
    try:
        _db_query("UPDATE productos SET activo=FALSE WHERE id_producto=%s", (product_id,), commit=True)
        flash("Producto retirado del catálogo.", "success")
    except Exception:
        flash("No se pudo retirar el producto.", "error")
    return redirect(url_for("admin_productos"))


@app.route("/admin/propuestas/<int:proposal_id>/publicar", methods=["POST"])
@admin_required
def admin_publicar_propuesta(proposal_id):
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = _cursor(conn)
        _execute(cur, "SELECT * FROM productos_proveedor WHERE id_producto_proveedor=%s AND estado='pendiente' FOR UPDATE", (proposal_id,))
        proposal = _fetchone(cur)
        if not proposal:
            flash("La propuesta ya no está pendiente.", "warning")
            return redirect(url_for("admin_dashboard"))
        _execute(cur,
            """UPDATE productos SET nombre=%s, marca=%s, descripcion=%s, precio=%s, stock=%s,
               color=%s, talla=%s, imagen_url=%s, activo=TRUE
               WHERE producto_proveedor_id=%s""",
            (proposal["nombre"], proposal["marca"], proposal["descripcion"], proposal["precio"], proposal["stock"], proposal["color"], proposal["talla"], proposal["imagen_url"], proposal_id),
        )
        if cur.rowcount == 0:
            _execute(cur,
                """INSERT INTO productos (nombre, marca, descripcion, precio, stock, color, talla, imagen_url, activo, producto_proveedor_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s)""",
                (proposal["nombre"], proposal["marca"], proposal["descripcion"], proposal["precio"], proposal["stock"], proposal["color"], proposal["talla"], proposal["imagen_url"], proposal_id),
            )
        _execute(cur, "UPDATE productos_proveedor SET estado='publicado' WHERE id_producto_proveedor=%s", (proposal_id,))
        conn.commit()
        flash("Producto del proveedor agregado al catálogo público.", "success")
    except Exception:
        if conn:
            conn.rollback()
        logging.exception("Could not publish provider product")
        flash("No se pudo publicar la propuesta.", "error")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/propuestas/<int:proposal_id>/rechazar", methods=["POST"])
@admin_required
def admin_rechazar_propuesta(proposal_id):
    try:
        _db_query("UPDATE productos_proveedor SET estado='rechazado' WHERE id_producto_proveedor=%s AND estado='pendiente'", (proposal_id,), commit=True)
        flash("Propuesta rechazada.", "info")
    except Exception:
        flash("No se pudo actualizar la propuesta.", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/proveedores", methods=["GET", "POST"])
@admin_required
def admin_proveedores():
    if request.method == "POST":
        password = request.form.get("contraseña", "")
        values = (
            request.form.get("razon_social", "").strip(), request.form.get("contacto", "").strip(),
            request.form.get("correo", "").strip().lower(), request.form.get("telefono", "").strip(),
            request.form.get("localidad", "").strip(),
        )
        if not values[0] or not values[2] or not is_valid_password(password):
            flash("Nombre y correo son obligatorios. La contraseña debe tener entre 8 y 20 caracteres e incluir mayúscula, minúscula, número y símbolo.", "error")
        else:
            try:
                _db_query(
                          """INSERT INTO proveedores
                              (razon_social, contacto, correo, telefono, localidad, password_hash, activo)
                              VALUES (%s, %s, %s, %s, %s, %s, TRUE)""",
                          values + (generate_password_hash(password),), commit=True,
                )
                flash("Proveedor agregado.", "success")
            except Exception:
                flash("No se pudo agregar el proveedor.", "error")
        return redirect(url_for("admin_proveedores"))
    try:
        proveedores = _db_query(
            """SELECT pr.*,
                      (SELECT COUNT(*) FROM productos_proveedor pp
                       WHERE pp.id_proveedor=pr.id_proveedor) AS total_productos
               FROM proveedores pr ORDER BY pr.razon_social""", fetch_all=True,
        )
    except Exception:
        proveedores = []
    return render_template("admin_proveedores.html", proveedores=proveedores)


@app.route("/admin/proveedores/<int:provider_id>/editar", methods=["POST"])
@admin_required
def admin_proveedor_editar(provider_id):
    password = request.form.get("contraseña", "")
    if password and not is_valid_password(password):
        flash("La contraseña debe tener entre 8 y 20 caracteres e incluir mayúscula, minúscula, número y símbolo.", "error")
        return redirect(url_for("admin_proveedores"))
    values = (
        request.form.get("razon_social", "").strip(), request.form.get("contacto", "").strip(),
        request.form.get("correo", "").strip().lower(), request.form.get("telefono", "").strip(),
        request.form.get("localidad", "").strip(),
        generate_password_hash(password) if password else None,
        provider_id,
    )
    try:
        _db_query("""UPDATE proveedores SET razon_social=%s, contacto=%s, correo=%s, telefono=%s,
                   localidad=%s, password_hash=COALESCE(%s, password_hash)
                   WHERE id_proveedor=%s""", values, commit=True)
        flash("Datos del proveedor actualizados.", "success")
    except Exception:
        flash("No se pudieron guardar los cambios.", "error")
    return redirect(url_for("admin_proveedores"))


@app.route("/admin/proveedores/<int:provider_id>/estado", methods=["POST"])
@admin_required
def admin_proveedor_estado(provider_id):
    active = request.form.get("activo") == "true"
    try:
        _db_query("UPDATE proveedores SET activo=%s WHERE id_proveedor=%s", (active, provider_id), commit=True)
        flash("Acceso del proveedor actualizado.", "success")
    except Exception:
        flash("No se pudo actualizar el acceso.", "error")
    return redirect(url_for("admin_proveedores"))


@app.route("/admin/proveedores/<int:provider_id>/eliminar", methods=["POST"])
@admin_required
def admin_proveedor_eliminar(provider_id):
    try:
        _db_query("DELETE FROM proveedores WHERE id_proveedor=%s", (provider_id,), commit=True)
        flash("Proveedor eliminado del directorio.", "success")
    except Exception:
        flash("No se pudo eliminar el proveedor.", "error")
    return redirect(url_for("admin_proveedores"))


@app.route("/admin/ventas")
@admin_required
def admin_ventas():
    try:
        pedidos = _db_query(
            """SELECT pe.id_pedido, pe.total, pe.estado, pe.metodo_pago, pe.fecha_creacion,
                      c.nombre, c.apellido, c.correo
               FROM pedidos pe JOIN clientes c ON c.id_clientes=pe.id_clientes
               ORDER BY pe.fecha_creacion DESC""", fetch_all=True,
        )
        for pedido in pedidos:
            pedido["items"] = _db_query(
                """SELECT p.nombre, p.marca, v.cantidad, v.talla, v.total FROM ventas v
                   JOIN productos p ON p.id_producto=v.id_producto WHERE v.id_pedido=%s""",
                (pedido["id_pedido"],), fetch_all=True,
            )
    except Exception:
        pedidos = []
    return render_template("admin_sales.html", pedidos=pedidos)


@app.route("/admin/ventas/<int:order_id>/estado", methods=["POST"])
@admin_required
def admin_estado_venta(order_id):
    state = request.form.get("estado", "")
    valid_states = {"pendiente", "pagado", "preparando", "enviado", "completado", "cancelado"}
    if state not in valid_states:
        flash("Estado de pedido inválido.", "error")
        return redirect(url_for("admin_ventas"))
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = _cursor(conn)
        _execute(cur, "SELECT estado FROM pedidos WHERE id_pedido=%s FOR UPDATE", (order_id,))
        order = _fetchone(cur)
        if not order:
            flash("No se encontró el pedido.", "error")
            return redirect(url_for("admin_ventas"))
        if order["estado"] == "cancelado" and state != "cancelado":
            flash("Un pedido cancelado no se puede reactivar.", "warning")
            return redirect(url_for("admin_ventas"))
        if state == "cancelado" and order["estado"] != "cancelado":
            _restore_order_stock(cur, order_id)
        _execute(cur, "UPDATE pedidos SET estado=%s WHERE id_pedido=%s", (state, order_id))
        conn.commit()
        flash("Estado de la venta actualizado.", "success")
    except Exception:
        if conn:
            conn.rollback()
        flash("No se pudo actualizar el estado.", "error")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
    return redirect(url_for("admin_ventas"))


@app.route("/proveedor")
@provider_required
def provider_dashboard():
    try:
        products = _db_query(
            "SELECT * FROM productos_proveedor WHERE id_proveedor=%s ORDER BY fecha_creacion DESC",
            (session["id_proveedor"],), fetch_all=True,
        )
    except Exception:
        products = []
    return render_template("provider_dashboard.html", products=products)


@app.route("/proveedor/productos", methods=["POST"])
@provider_required
def provider_product_create():
    values = _provider_product_values()
    if values:
        try:
            _db_query(
                """INSERT INTO productos_proveedor
                   (id_proveedor, nombre, marca, descripcion, precio, stock, color, talla, imagen_url, estado)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pendiente')""",
                (session["id_proveedor"],) + values, commit=True,
            )
            flash("Producto enviado a revisión administrativa.", "success")
        except Exception:
            flash("No se pudo guardar el producto.", "error")
    return redirect(url_for("provider_dashboard"))


def _provider_product_values():
    name = request.form.get("nombre", "").strip()
    brand = request.form.get("marca", "").strip()
    description = request.form.get("descripcion", "").strip()
    color = request.form.get("color", "").strip()
    size = request.form.get("talla", "").strip()
    image = request.form.get("imagen_url", "").strip()
    try:
        price = float(request.form.get("precio", ""))
        stock = int(request.form.get("stock", ""))
        if not name or not brand or not math.isfinite(price) or price < 0 or stock < 0:
            raise ValueError
        return name, brand, description, price, stock, color, size, image
    except (TypeError, ValueError):
        flash("Revisa nombre, marca, precio y stock.", "error")
        return None


@app.route("/proveedor/productos/<int:product_id>/editar", methods=["POST"])
@provider_required
def provider_product_edit(product_id):
    values = _provider_product_values()
    if values:
        try:
            _db_query(
                """UPDATE productos_proveedor SET nombre=%s, marca=%s, descripcion=%s, precio=%s, stock=%s,
                   color=%s, talla=%s, imagen_url=%s,
                   estado='pendiente'
                   WHERE id_producto_proveedor=%s AND id_proveedor=%s""",
                values + (product_id, session["id_proveedor"]), commit=True,
            )
            flash("Cambios guardados y enviados a revisión.", "success")
        except Exception:
            flash("No se pudo actualizar el producto.", "error")
    return redirect(url_for("provider_dashboard"))


@app.route("/proveedor/productos/<int:product_id>/eliminar", methods=["POST"])
@provider_required
def provider_product_delete(product_id):
    try:
        _db_query(
            "DELETE FROM productos_proveedor WHERE id_producto_proveedor=%s AND id_proveedor=%s",
            (product_id, session["id_proveedor"]), commit=True,
        )
        flash("Producto eliminado de tu lista.", "success")
    except Exception:
        flash("No se pudo eliminar el producto.", "error")
    return redirect(url_for("provider_dashboard"))


@app.route("/admin")
def admin_root():
    return redirect(url_for("admin_login"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5030")), debug=os.getenv("FLASK_DEBUG") == "1")
