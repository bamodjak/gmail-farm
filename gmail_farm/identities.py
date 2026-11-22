"""Random identity generation for signup attempts.

Everything here is randomly generated per attempt — no real person's
details, no hardcoded names. Usernames are built from the generated
name plus a random suffix so collisions are rare; the signup flow
itself is the authority on availability (see signup.py).
"""

from __future__ import annotations

import random
import secrets
import string
from dataclasses import dataclass

FIRST_NAMES = [
    "Liam", "Emma", "Noah", "Ava", "Lucas", "Mia", "Ethan", "Isabella",
    "Mason", "Sophia", "Logan", "Charlotte", "James", "Amelia", "Benjamin",
    "Harper", "Henry", "Evelyn", "Alexander", "Abigail", "Daniel", "Emily",
    "Matthew", "Elizabeth", "Joseph", "Sofia", "Samuel", "Avery", "David",
    "Ella", "John", "Scarlett", "Owen", "Grace", "Luke", "Chloe", "Gabriel",
    "Victoria", "Anthony", "Riley", "Dylan", "Aria", "Leo", "Lily",
    "Lincoln", "Aubrey", "Ryan", "Zoey", "Nathan", "Penelope",
]

LAST_NAMES = [
    "Smith", "Johnson", "Brown", "Taylor", "Miller", "Davis", "Garcia",
    "Wilson", "Anderson", "Thomas", "Moore", "Martin", "Jackson", "Lee",
    "Perez", "Thompson", "White", "Harris", "Clark", "Lewis", "Robinson",
    "Walker", "Hall", "Allen", "Young", "King", "Wright", "Scott", "Torres",
    "Nguyen", "Hill", "Flores", "Green", "Adams", "Nelson", "Baker",
    "Rivera", "Campbell", "Mitchell", "Carter", "Roberts", "Gomez",
]

GENDERS = ["male", "female", "rather_not_say"]

# Characters Google accepts in the username part (letters, digits, dots).
_USERNAME_ALPHABET = string.ascii_lowercase + string.digits


@dataclass
class Identity:
    """One randomly generated signup identity."""

    first_name: str
    last_name: str
    username: str          # without @gmail.com
    password: str
    birth_year: int
    birth_month: int       # 1-12
    birth_day: int         # 1-28 (always valid, avoids month-length issues)
    gender: str

    @property
    def email(self) -> str:
        return f"{self.username}@gmail.com"

    def public_summary(self) -> str:
        """Log-safe summary (no password)."""
        return f"{self.first_name} {self.last_name} <{self.email}>"


def random_password(length: int = 14) -> str:
    """Strong random password: mixed case + digits + symbols."""
    if length < 8:
        raise ValueError("password length must be >= 8")
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        # Require at least one of each class so Google accepts it.
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in "!@#$%^&*" for c in pw)):
            return pw


def random_username(first: str, last: str, rng: random.Random) -> str:
    """Build a plausible Gmail username from a name + random suffix.

    Google usernames must be >= 6 chars, letters/digits/dots only.
    """
    base = (first + "." + last).lower()
    base = "".join(c for c in base if c in _USERNAME_ALPHABET + ".")
    base = base.strip(".")
    suffix = "".join(rng.choice(_USERNAME_ALPHABET) for _ in range(rng.randint(2, 4)))
    # Occasionally use a year suffix instead of random letters.
    if rng.random() < 0.3:
        suffix = str(rng.randint(1990, 2025))
    username = f"{base}{suffix}"
    if len(username) < 6:
        username = username + "".join(rng.choice(_USERNAME_ALPHABET) for _ in range(6 - len(username)))
    return username[:30]


def generate_identity(rng: random.Random, password_length: int = 14,
                      birth_year_min: int = 1985,
                      birth_year_max: int = 2005) -> Identity:
    """Generate one complete random adult identity."""
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    return Identity(
        first_name=first,
        last_name=last,
        username=random_username(first, last, rng),
        password=random_password(password_length),
        birth_year=rng.randint(birth_year_min, birth_year_max),
        birth_month=rng.randint(1, 12),
        birth_day=rng.randint(1, 28),
        gender=rng.choice(GENDERS),
    )
