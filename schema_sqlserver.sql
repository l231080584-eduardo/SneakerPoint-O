CREATE TABLE clientes (
    id_clientes INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    nombre VARCHAR(120) NOT NULL,
    apellido VARCHAR(120) NOT NULL,
    correo VARCHAR(255) COLLATE Latin1_General_100_CI_AS NOT NULL UNIQUE,
    contraseña VARCHAR(255) NOT NULL,
    telefono VARCHAR(40) NOT NULL
);

CREATE TABLE proveedores (
    id_proveedor BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    razon_social VARCHAR(180) NOT NULL,
    contacto VARCHAR(140) NOT NULL DEFAULT '',
    correo VARCHAR(180) COLLATE Latin1_General_100_CI_AS NOT NULL DEFAULT '',
    telefono VARCHAR(40) NOT NULL DEFAULT '',
    localidad VARCHAR(120) NOT NULL DEFAULT '',
    notas VARCHAR(MAX) NOT NULL DEFAULT '',
    password_hash VARCHAR(255),
    activo BIT NOT NULL DEFAULT 1,
    fecha_alta DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);

CREATE UNIQUE INDEX proveedores_correo_unique_idx
    ON proveedores (correo) WHERE correo <> '';

CREATE TABLE productos_proveedor (
    id_producto_proveedor BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    id_proveedor BIGINT NOT NULL,
    nombre VARCHAR(180) NOT NULL,
    marca VARCHAR(120) NOT NULL,
    descripcion VARCHAR(MAX) NOT NULL DEFAULT '',
    precio DECIMAL(12,2) NOT NULL CHECK (precio >= 0),
    stock INT NOT NULL DEFAULT 0 CHECK (stock >= 0),
    color VARCHAR(80) NOT NULL DEFAULT '',
    talla VARCHAR(12) NOT NULL DEFAULT '',
    imagen_url VARCHAR(MAX) NOT NULL DEFAULT '',
    estado VARCHAR(20) NOT NULL DEFAULT 'pendiente'
        CHECK (estado IN ('pendiente', 'publicado', 'rechazado')),
    fecha_creacion DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT productos_proveedor_owner_fk FOREIGN KEY (id_proveedor)
        REFERENCES proveedores(id_proveedor) ON DELETE CASCADE
);

CREATE TABLE productos (
    id_producto INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    nombre VARCHAR(180) NOT NULL,
    marca VARCHAR(120) NOT NULL,
    descripcion VARCHAR(MAX) NOT NULL DEFAULT '',
    precio DECIMAL(12,2) NOT NULL DEFAULT 0 CHECK (precio >= 0),
    stock INT NOT NULL DEFAULT 0 CHECK (stock >= 0),
    color VARCHAR(80) NOT NULL DEFAULT '',
    talla VARCHAR(12) NOT NULL DEFAULT '',
    imagen_url VARCHAR(MAX) NOT NULL DEFAULT '',
    activo BIT NOT NULL DEFAULT 1,
    producto_proveedor_id BIGINT NULL,
    CONSTRAINT productos_proveedor_producto_fk FOREIGN KEY (producto_proveedor_id)
        REFERENCES productos_proveedor(id_producto_proveedor) ON DELETE SET NULL
);

CREATE TABLE pedidos (
    id_pedido BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    id_clientes INT NOT NULL,
    nombre VARCHAR(120) NOT NULL DEFAULT '',
    apellido VARCHAR(120) NOT NULL DEFAULT '',
    correo VARCHAR(255) NOT NULL DEFAULT '',
    telefono VARCHAR(40) NOT NULL DEFAULT '',
    calle VARCHAR(180) NOT NULL,
    numero_exterior VARCHAR(30) NOT NULL,
    numero_interior VARCHAR(30) NOT NULL DEFAULT '',
    codigo_postal VARCHAR(12) NOT NULL,
    metodo_pago VARCHAR(30) NOT NULL CHECK (metodo_pago IN ('Transferencia', 'Efectivo')),
    total DECIMAL(12,2) NOT NULL CHECK (total >= 0),
    estado VARCHAR(24) NOT NULL DEFAULT 'pendiente'
        CHECK (estado IN ('pendiente', 'pagado', 'preparando', 'enviado', 'completado', 'cancelado')),
    fecha_creacion DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT pedidos_cliente_fk FOREIGN KEY (id_clientes) REFERENCES clientes(id_clientes)
);

CREATE TABLE ventas (
    id_ventas BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    id_producto INT NOT NULL,
    id_clientes INT NOT NULL,
    id_pedido BIGINT NULL,
    cantidad INT NOT NULL CHECK (cantidad > 0),
    total DECIMAL(12,2) NOT NULL CHECK (total >= 0),
    talla VARCHAR(12) NOT NULL,
    fecha_salida DATE NOT NULL DEFAULT CONVERT(date, GETDATE()),
    referencia_pago VARCHAR(32) NULL,
    metodo_pago VARCHAR(30) NOT NULL DEFAULT 'Efectivo',
    CONSTRAINT ventas_producto_fk FOREIGN KEY (id_producto) REFERENCES productos(id_producto),
    CONSTRAINT ventas_cliente_fk FOREIGN KEY (id_clientes) REFERENCES clientes(id_clientes),
    CONSTRAINT ventas_pedido_fk FOREIGN KEY (id_pedido) REFERENCES pedidos(id_pedido)
);

CREATE INDEX pedidos_cliente_fecha_idx ON pedidos (id_clientes, fecha_creacion DESC);
CREATE INDEX productos_proveedor_owner_idx ON productos_proveedor (id_proveedor, fecha_creacion DESC);
CREATE INDEX productos_publicados_idx ON productos (activo, nombre);
CREATE INDEX ventas_pedido_idx ON ventas (id_pedido);
