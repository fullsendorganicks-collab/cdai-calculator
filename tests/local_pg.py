"""Throwaway local PostgreSQL for the calculator tests (same approach as the engine repo's local_test_pg.py).
start() initdb's a fresh cluster in a temp dir, creates the three calculator tables with the production columns, sets DATABASE_URL, and stops and
deletes the cluster at exit. It never connects to anything but that private socket-only server."""
import atexit, glob, os, shutil, socket, subprocess, sys, tempfile

DDL = """
CREATE TABLE calculator_leads (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL, company text NOT NULL, email text NOT NULL,
  inputs jsonb, results jsonb, email_sent boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE calculator_otps (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), email text NOT NULL, name text NOT NULL, company text NOT NULL, code text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL DEFAULT now() + interval '10 minutes',
  verified boolean NOT NULL DEFAULT false);
CREATE TABLE calculator_views (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), session_id text, viewed_at timestamptz NOT NULL DEFAULT now());
"""


def start():
    pg_bin = os.getenv("PG_BIN") or next(iter(sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)), None)
    if not pg_bin or not os.path.exists(os.path.join(pg_bin, "initdb")):
        print("FAIL: PostgreSQL server binaries not found (set PG_BIN)")
        sys.exit(1)          # a test that cannot start its database must fail, never pass having run nothing
    tmp = tempfile.mkdtemp(prefix="calc_test_pg_")
    data = os.path.join(tmp, "data")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    as_pg = ["runuser", "-u", "postgres", "--"] if os.geteuid() == 0 else []
    if as_pg:
        shutil.chown(tmp, user="postgres")

    def pg(*a):
        subprocess.run(as_pg + list(a), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    pg(os.path.join(pg_bin, "initdb"), "-D", data, "-A", "trust", "-U", "cdai")
    pg(os.path.join(pg_bin, "pg_ctl"), "-D", data, "-w", "-l", os.path.join(tmp, "pg.log"),
       "-o", f"-p {port} -k {tmp} -c listen_addresses='' -c max_connections=400", "start")

    def stop():
        try:
            pg(os.path.join(pg_bin, "pg_ctl"), "-D", data, "-m", "immediate", "stop")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    atexit.register(stop)
    dsn = f"postgresql://cdai@/postgres?host={tmp}&port={port}"
    import psycopg2
    c = psycopg2.connect(dsn); c.autocommit = True
    c.cursor().execute(DDL); c.close()
    os.environ["DATABASE_URL"] = dsn
    return dsn
