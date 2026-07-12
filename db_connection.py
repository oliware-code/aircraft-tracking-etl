import configparser
from pathlib import Path

import psycopg2

INI_PATH = Path(__file__).with_name("database.ini")


def load_config(section="RP5", path=INI_PATH):
    config = configparser.ConfigParser()
    config.read(path)
    if section not in config:
        raise ValueError(f"Section '{section}' not found in {path}")
    return dict(config[section])


def get_connection(section="RP5"):
    params = load_config(section)
    return psycopg2.connect(
        host=params["host"],
        dbname=params["database"],
        user=params["user"],
        password=params["password"],
        options=params.get("options"),
    )


if __name__ == "__main__":
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            print(cur.fetchone()[0])
    finally:
        conn.close()
