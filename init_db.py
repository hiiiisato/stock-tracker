"""
TiDB/MySQL スキーマ初期化。初回1回だけ実行する。
stock_tracker データベースと全テーブルを作成する。
"""
import os
import pymysql
from dotenv import load_dotenv

load_dotenv()

ssl_ca = os.environ.get("TIDB_SSL_CA", "")
ssl_config = {"ca": ssl_ca} if ssl_ca else {}

base_cfg = dict(
    host=os.environ["TIDB_HOST"],
    port=int(os.environ.get("TIDB_PORT", 4000)),
    user=os.environ["TIDB_USER"],
    password=os.environ["TIDB_PASSWORD"],
    ssl=ssl_config,
    charset="utf8mb4",
)

# データベース作成
conn = pymysql.connect(**base_cfg)
cur = conn.cursor()
cur.execute("CREATE DATABASE IF NOT EXISTS stock_tracker CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
conn.commit()
cur.close()
conn.close()
print("データベース stock_tracker を作成しました。")

# テーブル作成
conn = pymysql.connect(**base_cfg, database="stock_tracker")
cur = conn.cursor()

tables = [
    """
    CREATE TABLE IF NOT EXISTS markets (
        id   INT AUTO_INCREMENT PRIMARY KEY,
        code VARCHAR(10) NOT NULL,
        name VARCHAR(100),
        UNIQUE KEY uq_code (code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sectors (
        id   INT AUTO_INCREMENT PRIMARY KEY,
        code VARCHAR(10) NOT NULL,
        name VARCHAR(100),
        UNIQUE KEY uq_code (code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stocks (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        code          VARCHAR(10) NOT NULL,
        name          VARCHAR(200) NOT NULL,
        name_en       VARCHAR(200),
        market_id     INT,
        sector_id     INT,
        is_active     BOOLEAN DEFAULT TRUE,
        delisted_date DATE,
        created_at    DATETIME DEFAULT NOW(),
        updated_at    DATETIME DEFAULT NOW() ON UPDATE NOW(),
        UNIQUE KEY uq_code (code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trading_calendar (
        date       DATE NOT NULL PRIMARY KEY,
        is_holiday BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_prices (
        id             BIGINT AUTO_INCREMENT PRIMARY KEY,
        code           VARCHAR(10) NOT NULL,
        date           DATE NOT NULL,
        open           DECIMAL(18,4),
        high           DECIMAL(18,4),
        low            DECIMAL(18,4),
        close          DECIMAL(18,4),
        volume         BIGINT,
        turnover       BIGINT,
        adj_close      DECIMAL(18,4),
        adj_factor     DECIMAL(12,6) DEFAULT 1.0,
        change_pct     DECIMAL(12,4),
        is_upper_limit BOOLEAN DEFAULT FALSE,
        is_lower_limit BOOLEAN DEFAULT FALSE,
        UNIQUE KEY uq_code_date (code, date),
        INDEX idx_date (date),
        INDEX idx_code (code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rankings (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        period_type VARCHAR(10) NOT NULL,
        period_end  DATE NOT NULL,
        rank_type   VARCHAR(20) NOT NULL,
        `rank`      INT NOT NULL,
        code        VARCHAR(10) NOT NULL,
        value       DECIMAL(20,4),
        created_at  DATETIME DEFAULT NOW(),
        UNIQUE KEY uq_ranking (period_type, period_end, rank_type, `rank`)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fetch_logs (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        fetch_type    VARCHAR(50),
        status        VARCHAR(20),
        rows_upserted INT DEFAULT 0,
        started_at    DATETIME DEFAULT NOW(),
        finished_at   DATETIME,
        error_msg     TEXT
    )
    """,
]

for sql in tables:
    cur.execute(sql)

conn.commit()
cur.close()
conn.close()
print("全テーブル作成完了。")
print("\n次のステップ:")
print("  python master.py    # 銘柄マスタ・カレンダー取得")
print("  python daily_run.py # 価格データ取得・ランキング計算")
