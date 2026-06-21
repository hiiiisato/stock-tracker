import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

JQUANTS_API_KEY = os.environ["JQUANTS_API_KEY"]
JQUANTS_BASE_URL = "https://api.jquants.com/v2"
JQUANTS_HEADERS = {"x-api-key": JQUANTS_API_KEY}

DB_CONFIG = dict(
    host=os.environ["SUPABASE_DB_HOST"],
    port=5432,
    database="postgres",
    user="postgres",
    password=os.environ["SUPABASE_DB_PASSWORD"],
)


def get_conn():
    return psycopg2.connect(**DB_CONFIG)
