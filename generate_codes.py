import os
import sys
import random
import string
import datetime

# Add project root to sys.path to allow importing petey
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from petey import db as sqlite3

def generate_codes(count=5):
    try:
        conn = sqlite3.connect()
        c = conn.cursor()
        
        # Ensure table exists if DB is newly created and init_db hasn't run
        c.execute("""
            CREATE TABLE IF NOT EXISTS invite_codes (
                id          SERIAL PRIMARY KEY,
                code        VARCHAR(256) NOT NULL,
                used        INTEGER DEFAULT 0,
                created_at  VARCHAR(64) NOT NULL
            )
        """)
        
        print(f"Generating {count} single-use invite codes...")
        print("-" * 30)
        
        for i in range(count):
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            c.execute("INSERT INTO invite_codes (code, used, created_at) VALUES (?, 0, ?)", 
                      (code, datetime.datetime.utcnow().isoformat()))
            print(f"[{i+1}] {code}")
            
        conn.commit()
        print("-" * 30)
        print("All codes successfully added to the database!")
            
    except Exception as e:
        print(f"Error accessing database: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    count = 5
    if len(sys.argv) > 1:
        try:
            count = int(sys.argv[1])
        except ValueError:
            print("Please provide a valid number of codes to generate.")
            sys.exit(1)
            
    generate_codes(count)
