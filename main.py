"""
Main entry point — runs the Telegram bot and the API server in one process
"""
import os
import threading
import logging

# Configure logging before waitress.serve() calls basicConfig() at WARNING level
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per Telegram poll otherwise


def run_api():
    from waitress import serve
    from api import app
    port = int(os.getenv("PORT", 8000))
    logging.getLogger(__name__).info(f"API server starting on port {port}")
    serve(app, host="0.0.0.0", port=port, threads=8)


def run_bot():
    import bot
    bot.main()


if __name__ == "__main__":
    threading.Thread(target=run_api, daemon=True).start()
    run_bot()
