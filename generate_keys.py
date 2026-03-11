#!/usr/bin/env python3
"""
Helper script to generate secure SECRET_KEY and ENCRYPTION_KEY values
for the .env file.

Usage:
    python generate_keys.py
"""
import base64
import secrets

from cryptography.fernet import Fernet

secret_key = secrets.token_hex(32)
encryption_key = Fernet.generate_key().decode()

print("Add these to your .env file:\n")
print(f"SECRET_KEY={secret_key}")
print(f"ENCRYPTION_KEY={encryption_key}")
