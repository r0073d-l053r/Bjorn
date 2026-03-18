"""debug_schema.py - Dump RL table schemas to schema_debug.txt for quick inspection."""

import sqlite3
import os

db_path = "bjorn.db"

def check_schema():
    if not os.path.exists(db_path):
        print(f"Database {db_path} not found.")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    tables = ["rl_training_log", "rl_experiences"]
    
    with open("schema_debug.txt", "w") as f:
        for table in tables:
            f.write(f"\nSchema for {table}:\n")
            try:
                cursor.execute(f"PRAGMA table_info({table})")
                columns = cursor.fetchall()
                if not columns:
                    f.write("  (Table not found)\n")
                else:
                    for col in columns:
                        f.write(f"  - {col[1]} ({col[2]})\n")
            except Exception as e:
                f.write(f"  Error: {e}\n")

    conn.close()

if __name__ == "__main__":
    check_schema()
    print("Done writing to schema_debug.txt")
