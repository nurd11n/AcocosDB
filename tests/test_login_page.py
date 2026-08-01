from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings


@override_settings(AXES_ENABLED=False)
class LoginPageTests(TestCase):
    """Plain username + password login — OTP_ENABLED is False under the test
    settings module (config/settings/dev.py), so no code field/step applies
    here. See tests/test_otp.py for the OTP_ENABLED=True behavior."""

    def test_get_renders_split_layout(self):
        r = self.client.get("/login/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("login-photo", html)
        self.assertIn("login-wordmark", html)
        self.assertIn('name="next"', html)
        # No second-factor field on the page.
        self.assertNotIn('name="code"', html)
        self.assertNotIn('name="otp_token"', html)

    def test_unknown_user_generic_message(self):
        r = self.client.post("/login/", {"username": "nobody", "password": "x", "next": ""})
        html = r.content.decode()
        self.assertIn("Неверный логин или пароль.", html)
        self.assertNotIn("Please enter a correct", html)

    def test_wrong_password_same_generic_message(self):
        get_user_model().objects.create_user("alice", password="rightpass123")
        r = self.client.post("/login/", {"username": "alice", "password": "WRONG", "next": ""})
        self.assertIn("Неверный логин или пароль.", r.content.decode())

    def test_correct_credentials_log_in_and_reach_pos(self):
        get_user_model().objects.create_user("carol", password="rightpass123")
        r = self.client.post(
            "/login/", {"username": "carol", "password": "rightpass123", "next": ""}
        )
        self.assertRedirects(r, "/pos/", fetch_redirect_response=False)
        # Session is fully authenticated on a subsequent request — no code step.
        self.assertEqual(self.client.get("/pos/today/").status_code, 200)

    def test_open_redirect_blocked(self):
        get_user_model().objects.create_user("bob", password="rightpass123")
        r = self.client.post(
            "/login/", {"username": "bob", "password": "rightpass123", "next": "//evil.com/x"}
        )
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("evil.com", r["Location"])
        self.assertEqual(r["Location"], "/pos/")

    def test_anonymous_hit_on_pos_redirects_to_login(self):
        r = self.client.get("/pos/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login/", r["Location"])


@override_settings(AXES_ENABLED=False)
class LoginResilienceTests(TestCase):
    def test_login_get_touches_db_zero_times(self):
        # The login page must render with the database down: an anonymous GET
        # must not issue a single query (no session load, no user lookup).
        with self.assertNumQueries(0):
            r = self.client.get("/login/")
        self.assertEqual(r.status_code, 200)
