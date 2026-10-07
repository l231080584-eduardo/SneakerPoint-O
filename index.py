
from functools import wraps
import hashlib
import hmac
import logging
import math
import os
import re
from urllib.parse import urlsplit

import psycopg2
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for
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
if DB_ENGINE not in {"postgres", "sqlserver"}:
    raise ValueError("DB_ENGINE debe ser 'postgres' o 'sqlserver'.")


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
                "SELECT * FROM productos WHERE activo = TRUE AND (marca ILIKE %s OR nombre ILIKE %s) ORDER BY nombre",
                (term, term), fetch_all=True,
            )
        else:
            productos = _db_query("SELECT * FROM productos WHERE activo = TRUE ORDER BY nombre", fetch_all=True)
    except Exception:
        productos = []
        flash("El catálogo no está disponible por el momento.", "warning")
    return render_template("index.html", productos=productos, search_query=query)


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
            session.clear()
            session.update({"loggedin": True, "role": "customer", "id_cliente": customer["id_clientes"], "nombre_cliente": customer["nombre"]})
            if pending_items:
                session["cart"] = pending_items
            flash(f"Hola, {customer['nombre']}. Ya puedes completar tu compra.", "success")
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


@app.route("/add_cart", methods=["POST"])
def add_cart():
    try:
        product_id = int(request.form.get("id_producto", ""))
        quantity = int(request.form.get("cantidad", "1"))
        size = request.form.get("talla", "").strip()
        if quantity < 1 or not size:
            raise ValueError
        product = _db_query("SELECT id_producto FROM productos WHERE id_producto = %s AND activo = TRUE", (product_id,), fetch_one=True)
        if not product:
            flash("Este modelo ya no está disponible.", "error")
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
        locked_items = []
        for key, quantity in session.get("cart", {}).items():
            product_id_text, size = key.split("_", 1)
            quantity = int(quantity)
            if quantity < 1:
                raise ValueError("Cantidad inválida.")
            _execute(cur, "SELECT id_producto, nombre, precio, stock FROM productos WHERE id_producto = %s AND activo = TRUE FOR UPDATE", (int(product_id_text),))
            product = _fetchone(cur)
            if not product or product["stock"] < quantity:
                raise ValueError(f"No hay stock suficiente para {product['nombre'] if product else 'uno de los modelos'}.")
            total += product["precio"] * quantity
            locked_items.append((product, quantity, size))

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
            "products": _db_query("SELECT COUNT(*) AS value FROM productos", fetch_one=True)["value"],
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


@app.route("/admin/vista-prueba", methods=["POST"])
@admin_required
def toggle_role_preview():
    session["preview_user"] = not session.get("preview_user", False)
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/productos")
@admin_required
def admin_productos():
    try:
        productos = _db_query("SELECT * FROM productos WHERE activo=TRUE ORDER BY nombre", fetch_all=True)
    except Exception:
        productos = []
    return render_template("admin_productos.html", productos=productos)


@app.route("/admin/productos/nuevo", methods=["POST"])
@admin_required
def admin_producto_nuevo():
    values = _product_form_values()
    if not values:
        return redirect(url_for("admin_productos"))
    try:
        _db_query(
            """INSERT INTO productos (nombre, marca, descripcion, precio, stock, color, talla, imagen_url, activo)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)""", values, commit=True,
        )
        flash("Producto publicado en el catálogo.", "success")
    except Exception:
        flash("No se pudo crear el producto.", "error")
    return redirect(url_for("admin_productos"))


def _product_form_values():
    nombre = request.form.get("nombre", "").strip()
    marca = request.form.get("marca", "").strip()
    descripcion = request.form.get("descripcion", "").strip()
    color = request.form.get("color", "").strip()
    talla = request.form.get("talla", "").strip()
    imagen_url = request.form.get("imagen_url", "").strip()
    try:
        precio = float(request.form.get("precio", ""))
        stock = int(request.form.get("stock", ""))
        if not nombre or not marca or not math.isfinite(precio) or precio < 0 or stock < 0:
            raise ValueError
        return (nombre, marca, descripcion, precio, stock, color, talla, imagen_url)
    except (TypeError, ValueError):
        flash("Verifica nombre, marca, precio y existencias.", "error")
        return None


@app.route("/admin/productos/<int:product_id>/editar", methods=["POST"])
@admin_required
def admin_producto_editar(product_id):
    values = _product_form_values()
    if values:
        try:
            _db_query(
                """UPDATE productos SET nombre=%s, marca=%s, descripcion=%s, precio=%s, stock=%s,
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
            _execute(cur,
                """UPDATE productos p SET stock=p.stock + lines.cantidad
                   FROM (SELECT id_producto, SUM(cantidad) AS cantidad FROM ventas
                         WHERE id_pedido=%s GROUP BY id_producto) lines
                   WHERE p.id_producto=lines.id_producto""",
                (order_id,),
            )
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
