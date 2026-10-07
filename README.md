# Sneaker Point

Tienda minorista de tenis con catálogo público. Los compradores crean una cuenta para comprar; los proveedores proponen productos desde un portal separado; administración revisa las propuestas antes de publicarlas y gestiona catálogo, proveedores y ventas.

## Accesos

- Compradores: catálogo público en `/`, registro/login, carrito, checkout e historial de pedidos.
- Proveedores: información y solicitud de cuenta en `/proveedores`; administración habilita su acceso antes de usar `/proveedor/login`. Los productos enviados quedan privados hasta que administración los publica.
- Administración: acceso en `/admin/login`, dashboard en `/admin/dashboard`, CRUD de tenis/proveedores, aprobación de propuestas e historial de ventas. Los contactos de proveedores solo aparecen en sus pantallas privadas.

El checkout registra efectivo al recibir o transferencia. No se guardan datos de tarjeta ni se integra una pasarela de pagos.

## Base de datos

Selecciona el motor mediante `DB_ENGINE`. Para PostgreSQL ejecuta [schema.sql](schema.sql); para SQL Server crea primero la base y ejecuta [schema_sqlserver.sql](schema_sqlserver.sql). Ambos esquemas crean las tablas de clientes, productos, proveedores, propuestas, pedidos y ventas.

Con PostgreSQL y `psql` configurado:

```powershell
psql "$env:DATABASE_URL" -f schema.sql
```

Las cuentas antiguas que guardaban SHA-256 se migran al formato seguro de Werkzeug al iniciar sesión. Las cuentas nuevas usan Werkzeug desde el inicio. Las solicitudes de proveedor comienzan deshabilitadas y un administrador debe habilitarlas.

## Ejecución local

1. Instala dependencias: `pip install -r requirements.txt`.
2. Copia `.env.example` a `.env`, selecciona `DB_ENGINE=sqlserver` o `DB_ENGINE=postgres` y configura `SECRET_KEY` y las credenciales del motor. SQL Server requiere el controlador ODBC indicado por `DB_DRIVER`.
3. Ejecuta el script de esquema correspondiente en la base seleccionada.
4. Crea el hash de administración con `python scripts/hash_admin_password.py`; configura la salida como `ADMIN_PASSWORD_HASH` y define `ADMIN_EMAIL`.
5. Inicia con `python index.py` y abre `http://localhost:5030`.

No subas `.env` a Git. En producción usa una `SECRET_KEY` aleatoria y privada.

## Railway

1. Sube el proyecto a un repositorio Git, sin `.env`.
2. Crea un servicio desde el repositorio en Railway y añade PostgreSQL.
3. Configura `DB_ENGINE=postgres`, `DATABASE_URL`, `DB_SSLMODE=require`, `SECRET_KEY`, `ADMIN_EMAIL` y `ADMIN_PASSWORD_HASH` en variables del servicio.
4. Ejecuta `schema.sql` en la base alojada.
5. Railway usa [Procfile](Procfile) para iniciar Gunicorn. Genera el dominio público desde la configuración del servicio.
6. Entra al panel admin, revisa solicitudes de proveedor, habilita las cuentas y publica los modelos que quieres mostrar en el catálogo público.

Las imágenes deben usar URLs públicas. La foto de portada y la imagen genérica de tenis son recursos remotos de demostración.
