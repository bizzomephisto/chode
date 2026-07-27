# Petey

Petey is a modular Discord AI bot with long-term memory, configurable personas,
media generation tools, music playback, scheduled tasks, and a Flask-based web
control panel.

## Setup

1. Install Python 3.12 or newer and PostgreSQL.
2. Create and activate a virtual environment.
3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and fill in the required credentials.
5. Start the Discord bot:

   ```bash
   python main.py
   ```

6. Optionally start the web control panel:

   ```bash
   python run_web.py
   ```

The real `.env`, databases, server configuration, user images, caches, virtual
environment, and locally bundled FFmpeg executables are intentionally excluded
from version control.

## Project layout

- `main.py` — Discord bot entry point
- `petey/` — Petey's Python package containing bot services and cogs
- `web/` — web control panel
- `generate_codes.py` — invite-code utility

## Legacy CHODE reference

Petey is the active project and package. The former CHODE implementation is
obsolete and preserved only in Git history and the `archive/chode-legacy`
branch. The old `CHODEADMIN` Discord role name remains temporarily as a
compatibility alias for existing servers; new installations should use
`PETEYADMIN`.

## License

This project is available under the terms in [LICENSE](LICENSE).
