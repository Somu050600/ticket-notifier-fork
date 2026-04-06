"""
Generate VAPID keys for Web Push notifications.
Run this ONCE and copy the output into your .env file.
"""
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid, b64urlencode

v = Vapid()
v.generate_keys()

private_key = v.private_pem().decode()
public_key_bytes = v.public_key.public_bytes(
    serialization.Encoding.X962,
    serialization.PublicFormat.UncompressedPoint,
)
public_key = b64urlencode(public_key_bytes)

print("=" * 60)
print("Add these to your .env file (or set as environment variables):")
print("=" * 60)
print(f"\nVAPID_PRIVATE_KEY={private_key.strip()}")
print(f"\nVAPID_PUBLIC_KEY={public_key}")
print()
print("=" * 60)
print("One-liner for .env:")
print("=" * 60)
print(f'VAPID_PRIVATE_KEY="{private_key.strip()}"')
print(f'VAPID_PUBLIC_KEY="{public_key}"')
