import unittest

from hub import auth


class SessionTest(unittest.TestCase):
    secret = b"s" * 32

    def test_a_fresh_session_is_valid(self):
        self.assertTrue(auth.session_valid(self.secret, auth.make_session(self.secret, now=1000), now=1001))

    def test_a_changed_expiry_or_signature_is_rejected(self):
        good = auth.make_session(self.secret, now=1000)
        expires, _, signature = good.partition(".")
        for bad in [f"{int(expires) + 99999}.{signature}", f"{expires}.{'0' * 64}", "garbage", "", None, ".", "1.2.3"]:
            with self.subTest(bad=bad):
                self.assertFalse(auth.session_valid(self.secret, bad, now=1001))

    def test_another_secret_cannot_make_a_valid_session(self):
        self.assertFalse(auth.session_valid(self.secret, auth.make_session(b"x" * 32, now=1000), now=1001))

    def test_an_expired_session_is_rejected(self):
        value = auth.make_session(self.secret, now=1000)
        self.assertFalse(auth.session_valid(self.secret, value, now=1000 + auth.SESSION_SECONDS + 1))

    def test_token_check(self):
        digest = auth.hash_token("right")
        self.assertTrue(auth.token_matches("right", digest))
        self.assertFalse(auth.token_matches("wrong", digest))


class LimiterTest(unittest.TestCase):
    def test_five_failures_block_until_the_minute_passes(self):
        now = [0.0]
        limiter = auth.LoginLimiter(clock=lambda: now[0])
        for _ in range(auth.MAX_FAILS):
            self.assertFalse(limiter.blocked("a"))
            limiter.failed("a")
        self.assertTrue(limiter.blocked("a"))
        self.assertFalse(limiter.blocked("b"))
        now[0] += auth.WINDOW_SECONDS + 1
        self.assertFalse(limiter.blocked("a"))

    def test_success_clears_the_count(self):
        limiter = auth.LoginLimiter(clock=lambda: 0.0)
        for _ in range(4):
            limiter.failed("a")
        limiter.succeeded("a")
        limiter.failed("a")
        self.assertFalse(limiter.blocked("a"))


if __name__ == "__main__":
    unittest.main()
