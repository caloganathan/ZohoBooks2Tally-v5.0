"""Tests for secrets-at-rest encryption."""
from app.crypto import decrypt, encrypt


def test_encrypt_roundtrip():
    secret = "super-secret-refresh-token"
    ciphertext = encrypt(secret)
    assert ciphertext != secret
    assert ciphertext.startswith("enc::")
    assert decrypt(ciphertext) == secret


def test_encrypt_is_not_reversible_without_prefix_handling():
    ciphertext = encrypt("token")
    # The stored form must not contain the plaintext.
    assert "token" not in ciphertext


def test_decrypt_passthrough_for_legacy_plaintext():
    # Values stored before encryption existed have no prefix and must survive.
    assert decrypt("legacy-plaintext-value") == "legacy-plaintext-value"


def test_empty_values_pass_through():
    assert encrypt("") == ""
    assert encrypt(None) is None
    assert decrypt("") == ""
    assert decrypt(None) is None
