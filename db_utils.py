import os
from dotenv import load_dotenv
import logging

# Load environment variables from .env file if it exists
load_dotenv()

from urllib.parse import urlparse, quote
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL')

def fix_database_url(url: Optional[str]) -> Optional[str]:
    """Fix database URL by encoding special characters in password."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.password and any(c in parsed.password for c in ['*', '!', '@', '#', '$', '%', '^', '&', '(', ')', '=', '+', '?']):
            encoded_password = quote(parsed.password)
            fixed_url = f"postgresql://{parsed.username}:{encoded_password}@{parsed.hostname}:{parsed.port}{parsed.path}"
            return fixed_url
        return url
    except Exception as e:
        logger.error(f"Error fixing DB URL: {e}")
        return url

FIXED_DATABASE_URL = fix_database_url(DATABASE_URL)

# --- Global Database Pool ---
db_pool: Optional[ThreadedConnectionPool] = None

def ensure_tables_exist(conn):
    """Ensure necessary tables and columns exist."""
    try:
        cur = conn.cursor()
        
        # 1. Create movies table if not exists (with core columns)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL UNIQUE,
                url TEXT,
                file_id TEXT,
                description TEXT
            );
        """)

        # 2. Add metadata columns to movies if missing
        cols_to_add = [
            ("imdb_id", "TEXT"), ("poster_url", "TEXT"), ("year", "INTEGER DEFAULT 0"),
            ("genre", "TEXT"), ("rating", "TEXT"), ("category", "TEXT"),
            ("language", "TEXT"), ("extra_info", "TEXT"), ("\"cast\"", "TEXT"),
            ("backup_map", "JSONB DEFAULT '{}'::jsonb")
        ]
        for col_name, col_type in cols_to_add:
            cur.execute(f"ALTER TABLE movies ADD COLUMN IF NOT EXISTS {col_name} {col_type};")

        # 3. Create movie_files table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movie_files (
                id SERIAL PRIMARY KEY,
                movie_id INTEGER REFERENCES movies(id) ON DELETE CASCADE,
                quality TEXT NOT NULL,
                file_id TEXT,
                url TEXT,
                file_size TEXT,
                languages TEXT,
                extra_info TEXT,
                backup_map JSONB DEFAULT '{}'::jsonb,
                final_url TEXT,
                mirror TEXT,
                file_unique_id TEXT,
                UNIQUE(movie_id, quality, file_size)
            );
        """)
        
        try:
            # Attempt to drop the old constraint if it exists, and add the new one
            cur.execute("ALTER TABLE movie_files DROP CONSTRAINT IF EXISTS movie_files_movie_id_quality_key;")
            cur.execute("ALTER TABLE movie_files DROP CONSTRAINT IF EXISTS unique_movie_quality;")
            cur.execute("ALTER TABLE movie_files ADD CONSTRAINT movie_files_unique_size UNIQUE (movie_id, quality, file_size);")
        except Exception as e:
            conn.rollback()
            # It's fine if this fails on first run
            pass
            
        # Ensure new columns exist in movie_files
        mf_cols = [
            ("file_unique_id", "TEXT"),
            ("final_url", "TEXT"),
            ("mirror", "TEXT")
        ]
        for cname, ctype in mf_cols:
            try:
                cur.execute(f"ALTER TABLE movie_files ADD COLUMN IF NOT EXISTS {cname} {ctype};")
            except Exception as e:
                conn.rollback()
                pass
        
        # 4. Create movie_aliases table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movie_aliases (
                id SERIAL PRIMARY KEY,
                movie_id INTEGER REFERENCES movies(id) ON DELETE CASCADE,
                alias TEXT NOT NULL,
                UNIQUE(movie_id, alias)
            );
        """)

        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        logger.error(f"Error ensuring tables exist: {e}")


def init_db_pool():
    """Initialize the global database connection pool."""
    global db_pool
    if not FIXED_DATABASE_URL:
        logger.error("DATABASE_URL not set. Cannot initialize pool.")
        return
    try:
        db_pool = ThreadedConnectionPool(1, 5, FIXED_DATABASE_URL)
        logger.info("✅ Database connection pool initialized.")
        
        # Run table check ONCE at startup
        temp_conn = db_pool.getconn()
        try:
            ensure_tables_exist(temp_conn)
        finally:
            db_pool.putconn(temp_conn)
    except Exception as e:
        logger.error(f"❌ Failed to initialize database pool: {e}")


# Initialize pool on import
init_db_pool()


def get_db_connection():
    """Get a connection from the pool."""
    if not db_pool:
        init_db_pool()
        if not db_pool:
            return None
    try:
        conn = db_pool.getconn()
        # Table check moved to init_db_pool to avoid concurrent DDL locks
        return conn
    except Exception as e:
        logger.error(f"Error getting connection from pool: {e}")
        return None


def close_db_connection(conn):
    """Return a connection to the pool."""
    if db_pool and conn:
        try:
            db_pool.putconn(conn)
        except Exception as e:
            logger.error(f"Error returning connection to pool: {e}")


def upsert_movie_and_files(
    conn, 
    title: str, 
    description: str, 
    qualities: Dict[str, Any], 
    aliases_str: str, 
    movie_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> Optional[int]:
    """
    Insert or update movie, its multiple quality links/sizes, and aliases.
    accepts qualities as: {'Quality': {'url': '...', 'size': '...'}, ...}
    Returns (movie_id, is_new) or (None, False) on error.
    """
    if not title:
        return None, False
    
    meta = metadata or {}
    cur = conn.cursor()
    try:
        current_movie_id = movie_id

        # 1. Insert or Update Movie Record
        if current_movie_id:
            # Update existing
            cur.execute("""
                UPDATE movies 
                SET title = %s, description = %s,
                    year = COALESCE(NULLIF(%s, 0), year),
                    rating = COALESCE(NULLIF(%s, 'N/A'), rating),
                    genre = COALESCE(NULLIF(%s, 'Unknown'), genre),
                    poster_url = COALESCE(NULLIF(%s, ''), poster_url),
                    category = COALESCE(NULLIF(%s, 'Movies'), category),
                    "cast" = COALESCE(NULLIF(%s, ''), "cast"),
                    language = COALESCE(NULLIF(%s, ''), language)
                WHERE id = %s
            """, (
                title.strip(), description,
                meta.get('year', 0), meta.get('rating', 'N/A'),
                meta.get('genre', 'Unknown'), meta.get('poster_url', ''),
                meta.get('category', 'Movies'), meta.get('cast', ''),
                meta.get('language', ''), current_movie_id
            ))
        else:
            # Insert new or update on conflict
            cur.execute("""
                INSERT INTO movies (title, description, year, rating, genre, poster_url, category, "cast", language)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (title) DO UPDATE SET 
                    description = EXCLUDED.description,
                    year = COALESCE(NULLIF(EXCLUDED.year, 0), movies.year),
                    rating = COALESCE(NULLIF(EXCLUDED.rating, 'N/A'), movies.rating),
                    genre = COALESCE(NULLIF(EXCLUDED.genre, 'Unknown'), movies.genre),
                    poster_url = COALESCE(NULLIF(EXCLUDED.poster_url, ''), movies.poster_url),
                    category = COALESCE(NULLIF(EXCLUDED.category, 'Movies'), movies.category),
                    "cast" = COALESCE(NULLIF(EXCLUDED."cast", ''), movies."cast"),
                    language = COALESCE(NULLIF(EXCLUDED.language, ''), movies.language)
                RETURNING id
            """, (
                title.strip(), description,
                meta.get('year', 0), meta.get('rating', 'N/A'),
                meta.get('genre', 'Unknown'), meta.get('poster_url', ''),
                meta.get('category', 'Movies'), meta.get('cast', ''),
                meta.get('language', '')
            ))
            current_movie_id = cur.fetchone()[0] if cur.rowcount > 0 else None
            if current_movie_id:
                is_new = True
            else:
                cur.execute("SELECT id FROM movies WHERE title = %s", (title.strip(),))
                res = cur.fetchone()
                current_movie_id = res[0] if res else None
                is_new = False

        # 2. Upsert Qualities (Files/Links + Sizes)
        if qualities:
            for quality, data in qualities.items():
                link = ""
                size = ""

                # Handle data format (Dict or String)
                if isinstance(data, dict):
                    link = data.get('url', '').strip()
                    size = data.get('size', '').strip()
                else:
                    link = str(data).strip() if data else ""
                
                if not link:
                    continue

                # Determine if it's a File ID (BQAC...) or URL
                # extra_info aur languages bhi nikalein (agar dict mein hain)
                f_extra = data.get('extra_info', '') if isinstance(data, dict) else ''
                f_lang  = data.get('languages', '')  if isinstance(data, dict) else ''

                if any(link.startswith(prefix) for prefix in ("BQAC", "BAAC", "CAAC", "AQAC")):
                    cur.execute("""
                        INSERT INTO movie_files (movie_id, quality, file_id, url, file_size, extra_info, languages)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (movie_id, quality) 
                        DO UPDATE SET file_id = EXCLUDED.file_id, url = NULL,
                            file_size = EXCLUDED.file_size,
                            extra_info = COALESCE(NULLIF(EXCLUDED.extra_info,''), movie_files.extra_info),
                            languages  = COALESCE(NULLIF(EXCLUDED.languages,''),  movie_files.languages)
                    """, (current_movie_id, quality, link, None, size, f_extra, f_lang))
                else:
                    cur.execute("""
                        INSERT INTO movie_files (movie_id, quality, url, file_id, file_size, extra_info, languages)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (movie_id, quality) 
                        DO UPDATE SET url = EXCLUDED.url, file_id = NULL,
                            file_size = EXCLUDED.file_size,
                            extra_info = COALESCE(NULLIF(EXCLUDED.extra_info,''), movie_files.extra_info),
                            languages  = COALESCE(NULLIF(EXCLUDED.languages,''),  movie_files.languages)
                    """, (current_movie_id, quality, link, None, size, f_extra, f_lang))

        # 3. Add Aliases
        if aliases_str:
            aliases = [a.strip() for a in aliases_str.split(',') if a.strip()]
            for alias in aliases:
                cur.execute("""
                    INSERT INTO movie_aliases (movie_id, alias)
                    VALUES (%s, %s)
                    ON CONFLICT (movie_id, alias) DO NOTHING
                """, (current_movie_id, alias.lower()))

        conn.commit()
        return current_movie_id, is_new

    except Exception as e:
        conn.rollback()
        logger.error(f"Error upserting movie '{title}': {e}")
        return None, False
    finally:
        cur.close()

def get_all_movies(conn) -> List[Dict]:
    """Fetch all movies for admin list with file count."""
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # 👇 Updated Query: Counts files/links for each movie for the dashboard status
        cur.execute("""
            SELECT m.id, m.title, m.description,
                   (SELECT COUNT(*) FROM movie_files mf WHERE mf.movie_id = m.id) as file_count,
                   m.url, m.file_id
            FROM movies m 
            ORDER BY m.id DESC
        """)
        movies = cur.fetchall()
        cur.close()
        return movies
    except Exception as e:
        logger.error(f"Error fetching movies: {e}")
        return []

def get_movie_by_id(conn, movie_id: int) -> Optional[Dict]:
    """Fetch full movie details including qualities and aliases."""
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Get basic info
        cur.execute("SELECT * FROM movies WHERE id = %s", (movie_id,))
        movie = cur.fetchone()
        if not movie:
            return None

        # Get qualities/files
        cur.execute("SELECT quality, url, file_id, file_size FROM movie_files WHERE movie_id = %s", (movie_id,))
        files = cur.fetchall()
        
        # Reconstruct qualities dictionary for the form
        qualities_dict = {
            'Low Quality': {'url': '', 'size': ''},
            'SD Quality': {'url': '', 'size': ''},
            'Standard Quality': {'url': '', 'size': ''},
            'HD Quality': {'url': '', 'size': ''},
            '4K': {'url': '', 'size': ''}
        }
        
        for f in files:
            q_name = f['quality']
            # Determine value (File ID or URL)
            val = f['file_id'] if f['file_id'] else f['url']
            size = f['file_size'] if f['file_size'] else ''
            
            if q_name in qualities_dict:
                qualities_dict[q_name] = {'url': val, 'size': size}

        # Get aliases
        cur.execute("SELECT alias FROM movie_aliases WHERE movie_id = %s", (movie_id,))
        aliases_rows = cur.fetchall()
        aliases_str = ", ".join([row['alias'] for row in aliases_rows])

        # Convert RealDictRow to standard dict and add extras
        movie_data = dict(movie)
        movie_data['qualities'] = qualities_dict
        movie_data['aliases'] = aliases_str

        cur.close()
        return movie_data

    except Exception as e:
        logger.error(f"Error fetching movie {movie_id}: {e}")
        return None
