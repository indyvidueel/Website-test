#!/usr/bin/env python3
import argparse
import base64
import binascii
import errno
import getpass
import hmac
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent
DATABASE_DIR = PROJECT_ROOT / "data"
DATABASE_PATH = DATABASE_DIR / "news_articles.sqlite3"
MAX_REQUEST_SIZE = 4 * 1024 * 1024
MAX_IMAGE_SIZE = 2 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ALLOWED_CATEGORIES = {
    "Noord-Holland",
    "Amsterdam en omgeving",
    "Utrecht en Amersfoort",
    "Oost-Nederland",
    "Zuid-Holland- West-Brabant-Rivierenland",
}
BLOCKED_PUBLIC_PATHS = {"server.py", ".DS_Store", ".git", "__pycache__"}


def initialize_database():
    DATABASE_DIR.mkdir(exist_ok=True)
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    author TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    body TEXT NOT NULL,
                    image_mime TEXT,
                    image_data BLOB,
                    created_at TEXT NOT NULL
                )
                """
            )


def parse_article(payload):
    if not isinstance(payload, dict):
        raise ValueError("Article data must be a JSON object.")

    fields = {}
    limits = {"title": 140, "author": 80, "summary": 300, "body": 200000}
    for name, limit in limits.items():
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name.capitalize()} is required.")
        value = value.strip()
        if len(value) > limit:
            raise ValueError(f"{name.capitalize()} must be {limit} characters or fewer.")
        fields[name] = value

    category = payload.get("category")
    if not isinstance(category, str) or category not in ALLOWED_CATEGORIES:
        raise ValueError("Choose a valid article category.")
    fields["category"] = category

    image = payload.get("image")
    image_mime = None
    image_data = None
    if image is not None:
        if not isinstance(image, str):
            raise ValueError("The cover image is invalid.")
        match = re.fullmatch(r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/]*={0,2})", image)
        if not match or match.group(1) not in ALLOWED_IMAGE_TYPES:
            raise ValueError("Use a JPG, PNG, GIF, or WebP cover image.")
        try:
            image_data = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("The cover image data is invalid.") from None
        if not image_data:
            raise ValueError("The cover image is empty.")
        if len(image_data) > MAX_IMAGE_SIZE:
            raise ValueError("Cover images must be 2 MB or smaller.")
        image_mime = match.group(1)

    return fields, image_mime, image_data


class JournalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class ArticleRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_ROOT), **kwargs)

    def is_articles_endpoint(self):
        return urlsplit(self.path).path.rstrip("/") == "/api/articles"

    def article_id_from_path(self):
        match = re.fullmatch(r"/api/articles/(\d+)", urlsplit(self.path).path.rstrip("/"))
        return int(match.group(1)) if match else None

    def translate_path(self, path):
        translated = Path(super().translate_path(path))
        relative = Path(urlsplit(path).path.lstrip("/"))
        if relative.parts and (
            relative.parts[0] in BLOCKED_PUBLIC_PATHS
            or relative.name in BLOCKED_PUBLIC_PATHS
        ):
            return str(PROJECT_ROOT / "not-served")
        try:
            translated.resolve().relative_to(DATABASE_DIR.resolve())
        except ValueError:
            return str(translated)
        return str(DATABASE_DIR / "not-served")

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_article_payload(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            self.send_json(415, {"error": "Send article data as JSON."})
            return False, None

        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_json(400, {"error": "A valid Content-Length header is required."})
            return False, None
        if content_length < 0:
            self.send_json(400, {"error": "Invalid request size."})
            return False, None
        if content_length > MAX_REQUEST_SIZE:
            self.send_json(413, {"error": "Article data must be smaller than 4 MB."})
            return False, None

        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "Request body must contain valid JSON."})
            return False, None
        return True, payload

    def authorize_article_mutation(self):
        configured_password = os.environ.get("ARTICLE_PASSWORD")
        if not configured_password:
            self.send_json(503, {"error": "Article password is not configured on the server."})
            return False

        provided_password = self.headers.get("X-Article-Password", "")
        if not hmac.compare_digest(
            provided_password.encode("utf-8"),
            configured_password.encode("utf-8"),
        ):
            self.send_json(401, {"error": "The article password is incorrect."})
            return False
        return True

    def do_GET(self):
        request_path = urlsplit(self.path).path.rstrip("/")
        if request_path != "/api/articles":
            image_match = re.fullmatch(r"/api/articles/(\d+)/image", request_path)
            if image_match:
                self.send_article_image(int(image_match.group(1)))
                return
            return super().do_GET()

        try:
            with closing(sqlite3.connect(DATABASE_PATH)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT id, title, category, author, summary, body, created_at, image_mime
                    FROM articles
                    ORDER BY id DESC
                    """
                ).fetchall()
        except sqlite3.Error:
            self.send_json(500, {"error": "The article database could not be read."})
            return
        articles = []
        for row in rows:
            article = dict(row)
            has_image = article.pop("image_mime") is not None
            article["image_url"] = f"/api/articles/{article['id']}/image" if has_image else None
            articles.append(article)
        self.send_json(200, articles)

    def send_article_image(self, article_id):
        try:
            with closing(sqlite3.connect(DATABASE_PATH)) as connection:
                row = connection.execute(
                    "SELECT image_mime, image_data FROM articles WHERE id = ?",
                    (article_id,),
                ).fetchone()
        except sqlite3.Error:
            self.send_json(500, {"error": "The article image could not be read."})
            return

        if row is None or row[1] is None:
            self.send_json(404, {"error": "Article image not found."})
            return

        self.send_response(200)
        self.send_header("Content-Type", row[0])
        self.send_header("Content-Length", str(len(row[1])))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(row[1])

    def do_POST(self):
        if not self.is_articles_endpoint():
            self.send_json(404, {"error": "Not found."})
            return
        if not self.authorize_article_mutation():
            return

        has_payload, payload = self.read_article_payload()
        if not has_payload:
            return

        try:
            fields, image_mime, image_data = parse_article(payload)
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return

        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with closing(sqlite3.connect(DATABASE_PATH)) as connection:
                with connection:
                    cursor = connection.execute(
                        """
                        INSERT INTO articles (
                            title, category, author, summary, body, image_mime, image_data, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            fields["title"],
                            fields["category"],
                            fields["author"],
                            fields["summary"],
                            fields["body"],
                            image_mime,
                            image_data,
                            created_at,
                        ),
                    )
                    article_id = cursor.lastrowid
        except sqlite3.Error:
            self.send_json(500, {"error": "The article could not be saved to the database."})
            return

        self.send_json(201, {"id": article_id, "created_at": created_at})

    def do_PUT(self):
        article_id = self.article_id_from_path()
        if article_id is None:
            self.send_json(404, {"error": "Not found."})
            return
        if not self.authorize_article_mutation():
            return

        has_payload, payload = self.read_article_payload()
        if not has_payload:
            return

        try:
            fields, image_mime, image_data = parse_article(payload)
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return

        image_update = (
            ", image_mime = ?, image_data = ?"
            if "image" in payload
            else ""
        )
        values = [
            fields["title"],
            fields["category"],
            fields["author"],
            fields["summary"],
            fields["body"],
        ]
        if "image" in payload:
            values.extend((image_mime, image_data))
        values.append(article_id)

        try:
            with closing(sqlite3.connect(DATABASE_PATH)) as connection:
                with connection:
                    cursor = connection.execute(
                        f"""
                        UPDATE articles
                        SET title = ?, category = ?, author = ?, summary = ?, body = ?{image_update}
                        WHERE id = ?
                        """,
                        values,
                    )
        except sqlite3.Error:
            self.send_json(500, {"error": "The article could not be updated in the database."})
            return

        if cursor.rowcount == 0:
            self.send_json(404, {"error": "Article not found."})
            return
        self.send_json(200, {"id": article_id})

    def do_DELETE(self):
        article_id = self.article_id_from_path()
        if article_id is None:
            self.send_json(404, {"error": "Not found."})
            return
        if not self.authorize_article_mutation():
            return

        try:
            with closing(sqlite3.connect(DATABASE_PATH)) as connection:
                with connection:
                    cursor = connection.execute(
                        "DELETE FROM articles WHERE id = ?",
                        (article_id,),
                    )
        except sqlite3.Error:
            self.send_json(500, {"error": "The article could not be removed from the database."})
            return

        if cursor.rowcount == 0:
            self.send_json(404, {"error": "Article not found."})
            return
        self.send_json(200, {"id": article_id})


def main():
    parser = argparse.ArgumentParser(
        description="Run De Journal1st. Set ARTICLE_PASSWORD to allow article changes."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=2121, help="Port to listen on (default: 2121)")
    args = parser.parse_args()

    article_password = os.environ.get("ARTICLE_PASSWORD")
    if article_password is None:
        try:
            article_password = getpass.getpass("Article password: ")
        except EOFError:
            parser.error("Set ARTICLE_PASSWORD or start the server from an interactive terminal.")
    if not article_password:
        parser.error("The article password cannot be empty.")
    os.environ["ARTICLE_PASSWORD"] = article_password

    initialize_database()
    try:
        server = JournalHTTPServer((args.host, args.port), ArticleRequestHandler)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            print(
                f"Error: port {args.port} is already in use. "
                f"De Journal1st may already be running at http://{args.host}:{args.port}/",
                file=sys.stderr,
            )
            return
        raise
    print(f"De Journal1st is running at http://{args.host}:{args.port}/")
    print(f"Article database: {DATABASE_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
