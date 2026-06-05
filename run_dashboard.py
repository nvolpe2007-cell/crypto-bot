#!/usr/bin/env python3
"""
Standalone P&L Dashboard
Run this anytime (bot running or not) to open the live dashboard in your browser.

Usage:
    python run_dashboard.py
    python run_dashboard.py --port 8080
    python run_dashboard.py --no-browser
"""

import argparse
import asyncio
import logging
import os
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()


def main():
    parser = argparse.ArgumentParser(description='Crypto Bot Live P&L Dashboard')
    parser.add_argument('--port', type=int, default=8080, help='Port (default: 8080)')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Host (default: 0.0.0.0)')
    parser.add_argument('--no-browser', action='store_true', help='Do not auto-open browser')
    args = parser.parse_args()

    # Silence uvicorn startup noise
    logging.getLogger('uvicorn.access').setLevel(logging.WARNING)

    local_url = f'http://127.0.0.1:{args.port}'

    print()
    print('  ◆ CRYPTO BOT — LIVE DASHBOARD')
    print(f'  URL:  {local_url}')
    print(f'  LAN:  http://<your-ip>:{args.port}  (open on phone)')
    print()
    print('  Ctrl+C to stop')
    print()

    if not args.no_browser:
        # Open browser after a short delay so the server is ready
        async def _open_later():
            await asyncio.sleep(1.2)
            webbrowser.open(local_url)

    async def run():
        if not args.no_browser:
            asyncio.create_task(_open_later())

        import uvicorn
        from src.dashboard import app
        config = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level='warning',
        )
        server = uvicorn.Server(config)
        await server.serve()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print('\n  Dashboard stopped.')


if __name__ == '__main__':
    main()
