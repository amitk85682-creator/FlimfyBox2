import os
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL', "postgresql://postgres.zynzkjbwhcoppvykcqrc:ZoayKJsf1oUjoiAt@aws-1-ap-southeast-2.pooler.supabase.com:6543/postgres")

print("Fixing movie categories in the database...")
try:
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    
    # Update Bollywood movies
    cur.execute("UPDATE movies SET category = 'Bollywood' WHERE category = 'Movies' AND (language ILIKE '%Hindi%' OR language ILIKE '%hi%')")
    bolly_count = cur.rowcount
    
    # Update Hollywood movies
    cur.execute("UPDATE movies SET category = 'Hollywood' WHERE category = 'Movies' AND (language ILIKE '%English%' OR language ILIKE '%en%')")
    holly_count = cur.rowcount
    
    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ Updated {bolly_count} movies to Bollywood and {holly_count} to Hollywood.")
except Exception as e:
    print(f"❌ Error: {e}")
