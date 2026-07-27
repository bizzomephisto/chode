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
- `chode/` — legacy internal Python package containing Petey's bot services and cogs
- `web/` — web control panel
- `generate_codes.py` — invite-code utility

The internal package keeps its legacy name for import compatibility; the public
project and bot are named Petey.

## License

This project is available under the terms in [LICENSE](LICENSE).
