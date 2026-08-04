"""Password hashing + token encryption."""

from security import decrypt_token, encrypt_token, hash_password, verify_password


def test_password_hash_roundtrip():
    h = hash_password('secret123')
    assert h != 'secret123'
    assert verify_password(h, 'secret123')
    assert not verify_password(h, 'wrong')


def test_password_hashes_are_salted():
    h1 = hash_password('same')
    h2 = hash_password('same')
    assert h1 != h2  # random salt


def test_token_encrypt_roundtrip():
    token = 'sk-abc123-特殊字符'
    enc = encrypt_token(token)
    assert enc != token
    assert decrypt_token(enc) == token


def test_token_encrypt_empty():
    assert encrypt_token('') == ''
    assert decrypt_token('') == ''


def test_token_decrypt_corrupt():
    assert decrypt_token('!!!not-valid!!!') == ''
    assert decrypt_token('garbage') == ''
