#!/usr/bin/env python3
"""
Run once from the terminal to authenticate with Garmin Connect and save tokens.
After this, the Sync button in the app works without re-entering credentials.

Usage:
    python3 garmin_setup.py
"""
import os
import getpass
from garminconnect import Garmin

TOKENS = os.path.join(os.path.dirname(__file__), 'garmin_tokens')

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

email    = os.environ.get('GARMIN_EMAIL') or input('Garmin email: ')
password = os.environ.get('GARMIN_PASSWORD') or getpass.getpass('Garmin password: ')

print('Authenticating…')
api = Garmin(email=email, password=password)
api.login(tokenstore=TOKENS)
print(f'\nDone! Tokens saved to {TOKENS}')
print('You can now use the ↻ Sync Garmin button in the app.')
