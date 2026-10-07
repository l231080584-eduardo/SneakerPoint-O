CREATE TABLE IF NOT EXISTS clientes (
    id_clientes SERIAL PRIMARY KEY,
    nombre VARCHAR(120) NOT NULL,
    apellido VARCHAR(120) NOT NULL,
    correo VARCHAR(255) NOT NULL UNIQUE,
    contraseña VARCHAR(255) NOT NULL,
    telefono VARCHAR(40) NOT NULL
);

CREATE TABLE IF NOT EXISTS productos (
    id_producto SERIAL PRIMARY KEY,
    nombre VARCHAR(180) NOT NULL,
    marca VARCHAR(120) NOT NULL,
    descripcion TEXT NOT NULL DEFAULT '',
    precio NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (precio >= 0),
    stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
    color VARCHAR(80) NOT NULL DEFAULT '',
    talla VARCHAR(12) NOT NULL DEFAULT '',
    imagen_url TEXT NOT NULL DEFAULT '',
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    producto_proveedor_id BIGINT
);

ALTER TABLE clientes ALTER COLUMN contraseña TYPE VARCHAR(255);
ALTER TABLE productos ADD COLUMN IF NOT EXISTS imagen_url TEXT NOT NULL DEFAULT '';
ALTER TABLE productos ADD COLUMN IF NOT EXISTS activo BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE productos ADD COLUMN IF NOT EXISTS producto_proveedor_id BIGINT;

CREATE TABLE IF NOT EXISTS proveedores (
    id_proveedor BIGSERIAL PRIMARY KEY,
    razon_social VARCHAR(180) NOT NULL,
    contacto VARCHAR(140) NOT NULL DEFAULT '',
    correo VARCHAR(180) NOT NULL DEFAULT '',
    telefono VARCHAR(40) NOT NULL DEFAULT '',
    localidad VARCHAR(120) NOT NULL DEFAULT '',
    notas TEXT NOT NULL DEFAULT '',
    password_hash VARCHAR(255),
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    fecha_alta TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE proveedores ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);
ALTER TABLE proveedores ADD COLUMN IF NOT EXISTS fecha_alta TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
CREATE UNIQUE INDEX IF NOT EXISTS proveedores_correo_unique_idx
    ON proveedores (LOWER(correo)) WHERE correo <> '';

CREATE TABLE IF NOT EXISTS productos_proveedor (
    id_producto_proveedor BIGSERIAL PRIMARY KEY,
    id_proveedor BIGINT NOT NULL REFERENCES proveedores(id_proveedor) ON DELETE CASCADE,
    nombre VARCHAR(180) NOT NULL,
    marca VARCHAR(120) NOT NULL,
    descripcion TEXT NOT NULL DEFAULT '',
    precio NUMERIC(12, 2) NOT NULL CHECK (precio >= 0),
    stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
    color VARCHAR(80) NOT NULL DEFAULT '',
    talla VARCHAR(12) NOT NULL DEFAULT '',
    imagen_url TEXT NOT NULL DEFAULT '',
    estado VARCHAR(20) NOT NULL DEFAULT 'pendiente'
        CHECK (estado IN ('pendiente', 'publicado', 'rechazado')),
    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pedidos (
    id_pedido BIGSERIAL PRIMARY KEY,
    id_clientes INTEGER NOT NULL REFERENCES clientes(id_clientes),
    nombre VARCHAR(120) NOT NULL DEFAULT '',
    apellido VARCHAR(120) NOT NULL DEFAULT '',
    correo VARCHAR(255) NOT NULL DEFAULT '',
    telefono VARCHAR(40) NOT NULL DEFAULT '',
    calle VARCHAR(180) NOT NULL,
    numero_exterior VARCHAR(30) NOT NULL,
    numero_interior VARCHAR(30) NOT NULL DEFAULT '',
    codigo_postal VARCHAR(12) NOT NULL,
    metodo_pago VARCHAR(30) NOT NULL CHECK (metodo_pago IN ('Transferencia', 'Efectivo')),
    total NUMERIC(12, 2) NOT NULL CHECK (total >= 0),
    estado VARCHAR(24) NOT NULL DEFAULT 'pendiente'
        CHECK (estado IN ('pendiente', 'pagado', 'preparando', 'enviado', 'completado', 'cancelado')),
    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS nombre VARCHAR(120) NOT NULL DEFAULT '';
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS apellido VARCHAR(120) NOT NULL DEFAULT '';
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS correo VARCHAR(255) NOT NULL DEFAULT '';
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS telefono VARCHAR(40) NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS ventas (
    id_ventas BIGSERIAL PRIMARY KEY,
    id_producto INTEGER NOT NULL REFERENCES productos(id_producto),
    id_clientes INTEGER NOT NULL REFERENCES clientes(id_clientes),
    id_pedido BIGINT REFERENCES pedidos(id_pedido),
    cantidad INTEGER NOT NULL CHECK (cantidad > 0),
    total NUMERIC(12, 2) NOT NULL CHECK (total >= 0),
    talla VARCHAR(12) NOT NULL,
    fecha_salida DATE NOT NULL DEFAULT CURRENT_DATE,
    referencia_pago VARCHAR(32),
    metodo_pago VARCHAR(30) NOT NULL DEFAULT 'Efectivo'
);

ALTER TABLE ventas ADD COLUMN IF NOT EXISTS id_pedido BIGINT REFERENCES pedidos(id_pedido);
ALTER TABLE ventas ADD COLUMN IF NOT EXISTS metodo_pago VARCHAR(30) NOT NULL DEFAULT 'Efectivo';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'productos_proveedor_producto_fk'
    ) THEN
        ALTER TABLE productos
            ADD CONSTRAINT productos_proveedor_producto_fk
            FOREIGN KEY (producto_proveedor_id)
            REFERENCES productos_proveedor(id_producto_proveedor)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS pedidos_cliente_fecha_idx ON pedidos (id_clientes, fecha_creacion DESC);
CREATE INDEX IF NOT EXISTS productos_proveedor_owner_idx ON productos_proveedor (id_proveedor, fecha_creacion DESC);
CREATE INDEX IF NOT EXISTS productos_publicados_idx ON productos (activo, nombre);
CREATE INDEX IF NOT EXISTS ventas_pedido_idx ON ventas (id_pedido);
