# Learn2Play

## Run locally

1. Create and activate a Python virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. For local SQLite, verify that `DATABASE_URL` is unset and that the
   database is empty or already managed by Alembic. Apply the migrations with
   `python -m flask --app app db upgrade`. If the file already contains
   application tables without an Alembic revision, check its schema before
   applying migrations.
4. Run `python app.py` and open `http://127.0.0.1:5000`.

Without `DATABASE_URL`, the app uses `learn2play.db` in the project root.
`python app.py` reads `DATABASE_URL` from the process environment and does
not load `.env.local`. Application startup does not apply migrations.

## Run tests locally

From PowerShell, run `.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests`
with pytest and the project requirements installed in that virtual environment.
The suite uses disposable SQLite databases and, when PostgreSQL binaries are
available, starts its own loopback clusters for concurrency and migration
tests. Browser-script tests use Node.js. Run the suite without production
database or storage credentials.

## Supabase database

This app uses SQLAlchemy with Supabase's PostgreSQL database. In the Supabase dashboard, open **Connect** and copy the database connection string. Use the **Session pooler** connection string for a Render service if the direct connection is unreachable over IPv6. Keep the password in the URL encoded if it contains characters such as `@`, `:`, `/`, or `#`.

Set that full connection string as `DATABASE_URL` in the Render service's environment variables. Production startup requires a valid PostgreSQL URL and fails if the setting is missing or invalid. Startup validates the URL format without opening a database connection. Do not commit it or put it in frontend JavaScript. Manage the deployed database schema separately through Alembic.

## Deploy to Render

1. Push this repository to GitHub.
2. In Render, choose **New + → Blueprint** and connect this repository. Render reads `render.yaml` to create the web service.
3. Supply the Supabase PostgreSQL connection string as `DATABASE_URL` and a stable, high-entropy `SECRET_KEY` of at least 32 characters. Use the environment variable names in `render.yaml` to configure Supabase Storage and JaaS for those features.
4. After deployment, check `/healthz` on the assigned service URL. It verifies that Flask is running. Verify database connectivity and schema readiness separately.

For manual setup, use the same settings as `render.yaml`: Python 3.12.10, build command `pip install -r requirements.txt`, start command `gunicorn -w 2 --timeout 60 -b 0.0.0.0:$PORT wsgi:app`, and health-check path `/healthz`. Future pushes to the connected branch can deploy automatically through Render.
