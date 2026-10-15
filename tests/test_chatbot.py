import unittest
from decimal import Decimal
from unittest.mock import patch

import index


PRODUCTS = [
    {
        "id_producto": 1, "nombre": "Air Force 1", "marca": "Nike",
        "estilo": "Casual", "descripcion": "", "color": "blanco",
        "talla": "25", "precio": Decimal("1700.00"), "stock": 4, "activo": True,
    },
    {
        "id_producto": 2, "nombre": "Air Jordan 1 Mid", "marca": "Jordan",
        "estilo": "Moda", "descripcion": "", "color": "azul y blanco",
        "talla": "27", "precio": Decimal("3000.00"), "stock": 2, "activo": True,
    },
    {
        "id_producto": 3, "nombre": "Air Max 97", "marca": "Nike",
        "estilo": "Deportivo", "descripcion": "", "color": "blanco",
        "talla": "24", "precio": Decimal("1200.00"), "stock": 3, "activo": True,
    },
    {
        "id_producto": 4, "nombre": "Campus", "marca": "Adidas",
        "estilo": "Casual", "descripcion": "", "color": "gris",
        "talla": "26", "precio": Decimal("1500.00"), "stock": 2, "activo": True,
    },
    {
        "id_producto": 5, "nombre": "Chuck Taylor", "marca": "Converse",
        "estilo": "Casual", "descripcion": "", "color": "azul marino",
        "talla": "22", "precio": Decimal("1200.00"), "stock": 5, "activo": True,
    },
]


class ChatbotRegressionTests(unittest.TestCase):
    def setUp(self):
        self.client = index.app.test_client()
        self.queries = []
        self.style_available = True
        self.db_patch = patch.object(index, "_db_query", side_effect=self.fake_db_query)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

    def fake_db_query(self, sql, params=None, fetch_one=False, fetch_all=False, **kwargs):
        params = tuple(params or ())
        self.queries.append((sql, params))
        if "information_schema.columns" in sql:
            return {"available": self.style_available}
        if "FROM productos" in sql:
            if "id_producto IN" in sql:
                return [PRODUCTS[0]]
            matches = []
            patterns = [str(value)[1:-1].lower() for value in params if isinstance(value, str) and value.startswith("%") and value.endswith("%")]
            category_term_count = sql.count("(nombre ILIKE %s OR descripcion ILIKE %s)")
            category_patterns = patterns[:category_term_count * 2]
            if category_term_count:
                patterns = patterns[category_term_count * 2:]
            size_values = []
            if "LTRIM(RTRIM(talla)) IN" in sql:
                size_values = [
                    str(value) for value in params
                    if isinstance(value, str) and value.replace(".", "", 1).isdigit()
                ]
            max_price = next((value for value in params if isinstance(value, Decimal)), None)
            for product in PRODUCTS:
                haystack = " ".join(
                    str(product.get(key, "")) for key in
                    ("nombre", "marca", "estilo", "descripcion", "color")
                ).lower()
                name_description = " ".join(
                    str(product.get(key, "")) for key in ("nombre", "descripcion")
                ).lower()
                if category_patterns and not any(
                    pattern in name_description for pattern in category_patterns
                ):
                    continue
                if any(pattern not in haystack for pattern in patterns):
                    continue
                if size_values and product["talla"] not in size_values:
                    continue
                if max_price is not None and product["precio"] > max_price:
                    continue
                matches.append(product)
            if "SELECT nombre, marca, talla, stock" in sql:
                return [
                    {key: product[key] for key in ("nombre", "marca", "talla", "stock")}
                    for product in matches
                ]
            return matches[:5]
        if "FROM pedidos" in sql:
            self.assertEqual(params, (7,))
            return [
                {"estado": "completado", "cantidad": 2},
                {"estado": "pagado", "cantidad": 1},
                {"estado": "cancelado", "cantidad": 1},
            ]
        raise AssertionError(f"Unexpected database query: {sql}")

    def ask(self, message, role=None, session_values=None, history=None):
        with self.client.session_transaction() as flask_session:
            flask_session.clear()
            if role:
                flask_session.update(loggedin=True, role=role)
            if session_values:
                flask_session.update(session_values)
        return self.client.post(
            "/api/chatbot",
            json={"message": message, "history": history or []},
        )

    def test_brand_questions_match_catalog_columns_and_active_filter(self):
        cases = (
            ("¿Tienes tenis de la marca Nike?", "Air Force 1", "%nike%"),
            ("¿Tienes tenis de la marca Adidas?", "Campus", "%adidas%"),
            ("¿Tienes Converse?", "Chuck Taylor", "%converse%"),
        )
        for question, expected_model, expected_pattern in cases:
            with self.subTest(question=question):
                self.queries.clear()
                response = self.ask(question)
                self.assertEqual(response.status_code, 200)
                self.assertIn(expected_model, response.json["reply"])
                sql, params = self.queries[-1]
                self.assertIn("activo = TRUE", sql)
                self.assertIn("marca ILIKE %s", sql)
                self.assertIn(expected_pattern, params)
                self.assertNotIn("%marca%", params)

    def test_size_search_accepts_decimal_and_returns_matching_size(self):
        response = self.ask("¿Qué tenis hay en talla 22?")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Chuck Taylor", response.json["reply"])
        self.assertIn("talla 22", response.json["reply"])
        sql, params = self.queries[-1]
        self.assertIn("LTRIM(RTRIM(talla)) IN", sql)
        self.assertIn("22", params)

    def test_brand_and_size_filters_are_combined(self):
        response = self.ask("¿Tienen Nike en talla 25?")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Air Force 1", response.json["reply"])
        self.assertIn("talla 25", response.json["reply"])
        sql, params = self.queries[-1]
        self.assertIn("marca ILIKE %s", sql)
        self.assertIn("LTRIM(RTRIM(talla)) IN", sql)
        self.assertIn("%nike%", params)

    def test_follow_up_size_keeps_brand_context(self):
        response = self.ask("¿Y en talla 25?", history=["¿Tienes tenis Nike?"])
        self.assertEqual(response.status_code, 200)
        self.assertIn("Air Force 1", response.json["reply"])
        self.assertIn("%nike%", self.queries[-1][1])

    def test_categories_use_saved_style_not_assumptions(self):
        for question, category in (
            ("¿Hay tenis casuales?", "%Casual%"),
            ("¿Cuáles son los tenis deportivos?", "%Deportivo%"),
            ("¿Tienen tenis infantiles?", "%Infantil%"),
        ):
            with self.subTest(question=question):
                self.queries.clear()
                response = self.ask(question)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(self.queries)
                self.assertIn("estilo ILIKE %s", self.queries[-1][0])
                self.assertIn(category, self.queries[-1][1])
        self.queries.clear()
        response = self.ask("¿Qué tenis están de moda?")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Air Jordan 1 Mid", response.json["reply"])
        self.assertIn("estilo ILIKE %s", self.queries[-1][0])
        self.assertIn("%Moda%", self.queries[-1][1])

    def test_catalog_search_works_when_deployed_database_lacks_style_column(self):
        self.style_available = False
        response = self.ask("¿Tienes tenis Nike?")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Air Force 1", response.json["reply"])
        self.assertNotIn("estilo ILIKE", self.queries[-1][0])

        self.queries.clear()
        response = self.ask("¿Hay tenis casuales?")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("estilo ILIKE", self.queries[-1][0])
        self.assertIn("descripcion ILIKE %s", self.queries[-1][0])
        self.assertIn("categoría “Casual”", response.json["reply"])

    def test_child_category_fallback_requires_evidence_in_name_or_description(self):
        self.style_available = False
        child = {
            **PRODUCTS[0],
            "nombre": "Kids Runner",
            "marca": "Nike",
            "descripcion": "Modelo junior para niños",
            "estilo": "",
        }
        PRODUCTS.append(child)
        self.addCleanup(PRODUCTS.remove, child)
        response = self.ask("¿Tienen tenis infantiles?")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Kids Runner", response.json["reply"])
        self.assertIn("nombre ILIKE %s OR descripcion ILIKE %s", self.queries[-1][0])

    def test_storefront_search_also_works_without_style_column(self):
        self.style_available = False
        response = self.client.get("/?q=Nike")
        self.assertEqual(response.status_code, 200)
        sql, params = self.queries[-1]
        self.assertNotIn("estilo ILIKE", sql)
        self.assertEqual(params, ("%Nike%", "%Nike%", "%Nike%"))

    def test_social_questions_never_query_catalog(self):
        response = self.ask("¿Tienen Facebook?")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Facebook", response.json["reply"])
        self.assertEqual(self.queries, [])

    def test_switching_context_to_social_does_not_reuse_catalog_filter(self):
        response = self.ask(
            "¿Tienen Facebook?",
            history=["¿Tienes tenis Nike?"],
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Facebook", response.json["reply"])
        self.assertEqual(self.queries, [])

    def test_store_information_faqs_do_not_query_catalog(self):
        cases = (
            ("¿Cuáles son sus horarios?", "lunes a viernes"),
            ("¿Cuánto cuesta el envío?", "$99 MXN"),
            ("¿Cómo funcionan las devoluciones?", "30 días naturales"),
            ("¿Cuáles son sus redes sociales?", "Facebook"),
            ("¿Qué métodos de pago aceptan?", "Mercado Pago"),
        )
        for question, expected in cases:
            with self.subTest(question=question):
                self.queries.clear()
                response = self.ask(question)
                self.assertEqual(response.status_code, 200)
                self.assertIn(expected, response.json["reply"])
                self.assertEqual(self.queries, [])

    def test_child_size_guide_and_inches_do_not_search_products(self):
        child = self.ask("Guía de tallas de niños")
        self.assertEqual(child.status_code, 200)
        self.assertIn("guía infantil", child.json["reply"].lower())
        inches = self.ask("2 pulgadas")
        self.assertEqual(inches.status_code, 200)
        self.assertIn("5.08 cm", inches.json["reply"])
        self.assertNotIn("talla 2", inches.json["reply"].lower())
        self.assertEqual(self.queries, [])

    def test_cart_uses_current_session_cart_items_and_price(self):
        response = self.ask(
            "¿Qué tenis tengo en el carrito?",
            role="customer",
            session_values={"id_cliente": 7, "cart": {"1_25": 2}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Air Force 1", response.json["reply"])
        self.assertIn("talla 25 × 2", response.json["reply"])
        self.assertIn("$1,700.00 MXN por par", response.json["reply"])
        self.assertNotIn("No encontré modelos", response.json["reply"])
        self.assertTrue(any("id_producto IN" in sql for sql, _ in self.queries))

    def test_guest_cart_uses_pending_session_cart_and_explains_login(self):
        response = self.ask(
            "¿Qué tenis tengo en el carrito?",
            session_values={"pending_items": {"1_25": 1}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Air Force 1", response.json["reply"])
        self.assertIn("Inicia sesión como cliente", response.json["reply"])

    def test_order_count_is_customer_scoped_and_excludes_cancellations_from_completed(self):
        response = self.ask(
            "¿Cuántas compras he hecho?",
            role="customer",
            session_values={"id_cliente": 7},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("3 pedido(s) no cancelado(s)", response.json["reply"])
        self.assertIn("2 aparecen como completados", response.json["reply"])
        sql, params = self.queries[-1]
        self.assertIn("WHERE id_clientes=%s", sql)
        self.assertEqual(params, (7,))

    def test_guest_order_count_requires_login(self):
        response = self.ask("¿Cuántas compras he hecho?")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Inicia sesión como cliente", response.json["reply"])
        self.assertEqual(self.queries, [])

    def test_order_database_error_is_not_reported_as_empty_history(self):
        with patch.object(index, "_db_query", side_effect=RuntimeError("database unavailable")):
            response = self.ask(
                "¿Cuántas compras he hecho?",
                role="customer",
                session_values={"id_cliente": 7},
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Todavía no tienes pedidos", response.json["error"])

    def test_purchase_help_does_not_read_cart_or_catalog(self):
        response = self.ask("¿Cómo agrego productos al carrito?")
        self.assertEqual(response.status_code, 200)
        self.assertIn("carrito temporal", response.json["reply"])
        self.assertEqual(self.queries, [])

    def test_unrelated_questions_do_not_query_catalog(self):
        for question in ("Te extraño", "¿Sabes qué es un commit?", "asdfghjkl"):
            with self.subTest(question=question):
                self.queries.clear()
                response = self.ask(question)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(
                    "asistente de Sneaker Point" in response.json["reply"]
                    or "No alcancé a entender" in response.json["reply"]
                )
                self.assertEqual(self.queries, [])


if __name__ == "__main__":
    unittest.main()
