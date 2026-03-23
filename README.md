# SpendSense

SpendSense is a minimal, mobile-friendly Flask transaction tracker that lets you log expenses and credits in natural language.

## Features

- Natural language expense input like `250 fuel` or `499 swiggy`
- Regex-based amount extraction
- Keyword-based smart category detection
- Recent expenses view on the home page
- Dashboard with total spending, category totals, and a simple chart
- Add custom categories from the UI with comma-separated keywords
- PostgreSQL-ready via `DATABASE_URL`, with SQLite fallback for local development
- Sign-in with Google or email/password

## Project Structure

- `app.py` - Flask app, routes, and dashboard queries
- `models.py` - SQLAlchemy transaction model
- `parser.py` - Natural language parsing and category detection
- `templates/` - Bootstrap-based UI
- `static/` - Lightweight styling

## Run Locally

1. Create and activate a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Start the app:

   ```bash
   export APP_ENV=dev
   python app.py
   ```

4. Open `http://127.0.0.1:5001`

## Environment Files

The app supports two small local env files:

- `.env` -> selects the active environment with `APP_ENV=dev` or `APP_ENV=prod`
- `.env.dev` -> uses `sqlite:///spendsense_dev.db`
- `.env.prod` -> uses `sqlite:///spendsense_prod.db`

The app loads `.env` first, then the matching env file.

Example:

```bash
APP_ENV=dev
```

Then run:

```bash
python app.py
```

## Database

- Local development defaults to SQLite at `spendsense.db`
- To use PostgreSQL, set `DATABASE_URL`, for example:

  ```bash
  export DATABASE_URL="postgresql://username:password@localhost/spendsense"
  ```

## Google Login Setup

Set these environment variables before starting the app:

```bash
export GOOGLE_CLIENT_ID="your-google-client-id"
export GOOGLE_CLIENT_SECRET="your-google-client-secret"
export GOOGLE_REDIRECT_URI="http://127.0.0.1:5001/auth/google/callback"
```

In Google Cloud Console, add the same callback URL to your OAuth redirect URIs.

## Email/Password Login

SpendSense now also supports basic email/password signup and signin as a fallback.

- Each user has their own account
- New transactions are saved against the signed-in user's `user_id`
- Older transactions without a `user_id` are left untouched
