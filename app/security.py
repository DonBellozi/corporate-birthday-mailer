import base64
import hashlib
import os
from cryptography.fernet import Fernet
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def _fernet() -> Fernet:
    secret = os.getenv("APP_SECRET_KEY", "CHANGE_ME")
    key = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))

def encrypt_value(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii") if value else ""

def decrypt_value(value: str) -> str:
    return _fernet().decrypt(value.encode("ascii")).decode("utf-8") if value else ""

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)
