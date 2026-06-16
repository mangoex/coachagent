import base64
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from config.settings import settings

def _get_key_bytes() -> bytes:
    # Ensure key is 32 bytes (256 bits)
    try:
        key_bytes = base64.urlsafe_b64decode(settings.ENCRYPTION_KEY)
        if len(key_bytes) != 32:
            # Fallback to derivation or padding if key is not exactly 32 bytes
            key_bytes = settings.ENCRYPTION_KEY.encode().ljust(32, b'\0')[:32]
        return key_bytes
    except Exception:
        return settings.ENCRYPTION_KEY.encode().ljust(32, b'\0')[:32]

def encrypt_token(plain_text: str) -> str:
    """
    Encrypts a plain text string using AES-256-GCM.
    Returns a url-safe base64 encoded string containing the nonce and ciphertext.
    """
    if not plain_text:
        return ""
    key = _get_key_bytes()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # GCM standard 12-byte nonce
    ciphertext = aesgcm.encrypt(nonce, plain_text.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("utf-8")

def decrypt_token(cipher_text: str) -> str:
    """
    Decrypts a url-safe base64 encoded string containing the nonce and ciphertext using AES-256-GCM.
    """
    if not cipher_text:
        return ""
    key = _get_key_bytes()
    aesgcm = AESGCM(key)
    try:
        data = base64.urlsafe_b64decode(cipher_text.encode("utf-8"))
        if len(data) < 12:
            raise ValueError("Cipher text too short (missing nonce)")
        nonce = data[:12]
        ciphertext = data[12:]
        decrypted = aesgcm.decrypt(nonce, ciphertext, None)
        return decrypted.decode("utf-8")
    except Exception as e:
        raise ValueError(f"Decryption failed: {str(e)}")
