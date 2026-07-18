from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model


@override_settings(AXES_ENABLED=False)
class LoginPageTests(TestCase):
    def test_get_renders_split_layout(self):
        r = self.client.get("/login/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("login-photo", html)
        self.assertIn("login-wordmark", html)
        self.assertIn('name="next"', html)

    def test_unknown_user_generic_message(self):
        r = self.client.post("/login/", {"username": "nobody", "password": "x", "next": ""})
        html = r.content.decode()
        self.assertIn("Неверный логин, пароль или код.", html)
        self.assertNotIn("Please enter a correct", html)

    def test_wrong_password_same_message(self):
        get_user_model().objects.create_user("alice", password="rightpass123")
        r = self.client.post("/login/", {"username": "alice", "password": "WRONG", "next": ""})
        html = r.content.decode()
        self.assertIn("Неверный логин, пароль или код.", html)

    def test_open_redirect_blocked(self):
        get_user_model().objects.create_user("bob", password="rightpass123")
        r = self.client.post("/login/", {"username": "bob", "password": "rightpass123", "next": "//evil.com/x"})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("evil.com", r["Location"])
        self.assertEqual(r["Location"], "/pos/")


@override_settings(AXES_ENABLED=False, OTP_ENABLED=True)
class LoginOTPTests(TestCase):
    def test_correct_password_wrong_code_same_generic_message(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        user = get_user_model().objects.create_user("carol", password="rightpass123")
        TOTPDevice.objects.create(user=user, name="phone", confirmed=True)
        r = self.client.post(
            "/login/",
            {"username": "carol", "password": "rightpass123", "otp_token": "000000", "next": ""},
        )
        html = r.content.decode()
        # Must NOT reveal that the password was correct / that only the code failed:
        # the same single message as an unknown-user attempt, no token-specific error.
        self.assertIn("Неверный логин, пароль или код.", html)
        for leak in ("invalid token", "неверный код", "код неверен", "enter a token"):
            self.assertNotIn(leak, html.lower())


@override_settings(AXES_ENABLED=False)
class LoginResilienceTests(TestCase):
    def test_login_get_touches_db_zero_times(self):
        # The login page must render with the database down: an anonymous GET
        # must not issue a single query (no session load, no user lookup).
        with self.assertNumQueries(0):
            r = self.client.get("/login/")
        self.assertEqual(r.status_code, 200)

    def test_totp_field_has_one_time_code_attrs(self):
        with override_settings(OTP_ENABLED=True):
            html = self.client.get("/login/").content.decode()
        self.assertIn('autocomplete="one-time-code"', html)
        self.assertIn('inputmode="numeric"', html)
