from config.settings import *  # noqa: F401,F403


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "test_db.sqlite3",
    },
    "legacy": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "test_legacy.sqlite3",
    },
}

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]
