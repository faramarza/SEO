#!/usr/bin/env python3
"""
Google Ads OAuth2 Authentication Helper

Run this script to generate a refresh token for Google Ads API.

Usage:
    python scripts/google_ads_auth.py --client-id YOUR_CLIENT_ID --client-secret YOUR_SECRET

Or with a client_secrets.json file:
    python scripts/google_ads_auth.py --client-secrets /path/to/client_secrets.json
"""

import argparse
import json
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, parse_qs, urlparse
import threading
import time


# Google OAuth2 endpoints
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Google Ads API scope
SCOPES = ["https://www.googleapis.com/auth/adwords"]

# Local callback server
REDIRECT_URI = "http://localhost:8080/callback"


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handle OAuth callback."""

    auth_code = None

    def do_GET(self):
        """Handle GET request from OAuth callback."""
        parsed = urlparse(self.path)

        if parsed.path == "/callback":
            params = parse_qs(parsed.query)

            if "code" in params:
                OAuthCallbackHandler.auth_code = params["code"][0]
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(b"""
                    <html>
                    <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                        <h1 style="color: green;">Success!</h1>
                        <p>Authorization code received. You can close this window.</p>
                        <p>Return to your terminal to see the refresh token.</p>
                    </body>
                    </html>
                """)
            else:
                error = params.get("error", ["Unknown error"])[0]
                self.send_response(400)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(f"""
                    <html>
                    <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                        <h1 style="color: red;">Error</h1>
                        <p>{error}</p>
                    </body>
                    </html>
                """.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress logging."""
        pass


def get_auth_url(client_id: str) -> str:
    """Generate OAuth2 authorization URL."""
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",  # Force consent to get refresh token
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(client_id: str, client_secret: str, auth_code: str) -> dict:
    """Exchange authorization code for tokens."""
    import urllib.request

    data = urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": auth_code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }).encode()

    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode())


def main():
    parser = argparse.ArgumentParser(description="Generate Google Ads API refresh token")
    parser.add_argument("--client-id", help="OAuth2 Client ID")
    parser.add_argument("--client-secret", help="OAuth2 Client Secret")
    parser.add_argument("--client-secrets", help="Path to client_secrets.json file")

    args = parser.parse_args()

    # Load credentials
    client_id = args.client_id
    client_secret = args.client_secret

    if args.client_secrets:
        with open(args.client_secrets) as f:
            data = json.load(f)
            # Handle both "installed" and "web" app types
            creds = data.get("installed") or data.get("web")
            if creds:
                client_id = creds["client_id"]
                client_secret = creds["client_secret"]
            else:
                print("Error: Invalid client_secrets.json format")
                return

    if not client_id or not client_secret:
        print("Error: Please provide --client-id and --client-secret, or --client-secrets")
        print("\nUsage:")
        print("  python scripts/google_ads_auth.py --client-id YOUR_ID --client-secret YOUR_SECRET")
        print("  python scripts/google_ads_auth.py --client-secrets /path/to/client_secrets.json")
        return

    print("=" * 60)
    print("Google Ads API - OAuth2 Authentication")
    print("=" * 60)
    print()

    # Start local server
    server = HTTPServer(("localhost", 8080), OAuthCallbackHandler)
    server_thread = threading.Thread(target=server.handle_request)
    server_thread.start()

    # Generate and open auth URL
    auth_url = get_auth_url(client_id)
    print("Opening browser for authentication...")
    print()
    print("If browser doesn't open, visit this URL manually:")
    print(auth_url)
    print()

    webbrowser.open(auth_url)

    # Wait for callback
    print("Waiting for authorization...")
    server_thread.join(timeout=120)  # 2 minute timeout

    if not OAuthCallbackHandler.auth_code:
        print("Error: No authorization code received (timeout)")
        return

    print("Authorization code received!")
    print()

    # Exchange code for tokens
    print("Exchanging code for tokens...")
    try:
        tokens = exchange_code_for_tokens(client_id, client_secret, OAuthCallbackHandler.auth_code)
    except Exception as e:
        print(f"Error exchanging code: {e}")
        return

    refresh_token = tokens.get("refresh_token")

    if not refresh_token:
        print("Error: No refresh token received")
        print("Response:", tokens)
        return

    print()
    print("=" * 60)
    print("SUCCESS! Here's your refresh token:")
    print("=" * 60)
    print()
    print(refresh_token)
    print()
    print("=" * 60)
    print()
    print("Now create your google-ads.yaml file:")
    print()
    print(f"""developer_token: YOUR_DEVELOPER_TOKEN
client_id: {client_id}
client_secret: {client_secret}
refresh_token: {refresh_token}
use_proto_plus: True
""")
    print()
    print("Save this as ~/google-ads.yaml or in your project directory")


if __name__ == "__main__":
    main()
