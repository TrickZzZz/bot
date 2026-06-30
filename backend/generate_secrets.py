# Generate the two required secrets and write a .env file.
#
# Usage:
#   bash generate_secrets.sh
import secrets
from cryptography.fernet import Fernet

jwt_secret = secrets.token_urlsafe(64)
enc_key = Fernet.generate_key().decode()

with open(".env", "w") as f:
    f.write(f"JWT_SECRET_KEY={jwt_secret}\n")
    f.write(f"ACCOUNT_ENCRYPTION_KEY={enc_key}\n")
    f.write("DATABASE_URL=sqlite:///./accounts.db\n")

print("Wrote .env with a fresh JWT_SECRET_KEY and ACCOUNT_ENCRYPTION_KEY.")
print("Keep this file out of version control. Losing ACCOUNT_ENCRYPTION_KEY")
print("makes all stored account passwords permanently unrecoverable.")
