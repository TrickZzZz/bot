import getpass
from dotenv import load_dotenv

# 1. Load environment variables FIRST so your security.py doesn't crash
load_dotenv()

# 2. Import your app modules
from app.database import SessionLocal
from app.security import hash_password
from app.models import User  # -> IMPORTANT: Adjust 'User' if your model is named differently

def create_seed_account():
    print("=== Create Seed Account ===")
    
    # 3. Get credentials interactively
    username = input("Enter new admin username: ")
    password = getpass.getpass("Enter new admin password: ")
    
    if not username or not password:
        print("Username and password cannot be empty. Aborting.")
        return

    # 4. Hash the password using your passlib context
    hashed_pw = hash_password(password)

    # 5. Open a database session
    db = SessionLocal()
    try:
        # Check if user already exists
        existing_user = db.query(User).filter(User.username == username).first()
        if existing_user:
            print(f"Error: An account with the username '{username}' already exists.")
            return

        # 6. Create the user record
        # Note: Adjust these field names (e.g., 'username', 'hashed_password') 
        # to match exactly what is defined in your SQLAlchemy model
        new_user = User(
            username=username,
            password_hash=hashed_pw,
            # is_active=True,  <-- Uncomment if your model has this field
            # is_superuser=True <-- Uncomment if your model has this field
        )

        # 7. Save to database
        db.add(new_user)
        db.commit()
        print(f"Success! Seed account '{username}' has been securely created.")

    except Exception as e:
        db.rollback()
        print(f"An error occurred while writing to the database: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    create_seed_account()