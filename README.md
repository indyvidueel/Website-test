# De Journal1st

A responsive Dutch regional news website with a light/dark theme, article search, category filters, and a small newsroom editor.

> **Testing project:** De Journal1st is currently a personal testing project. It is not production-ready and should not be used to publish or manage sensitive information.

## Features

- Regional news categories
- Search and category filtering
- Full article dialog
- Light and dark themes
- Article submission with optional cover images
- Password-protected article creation, editing, and removal
- SQLite article storage
- Responsive desktop and mobile layouts

## Technology

- HTML and CSS
- Vanilla JavaScript
- Python 3 standard library
- SQLite

No package installation or frontend build step is required.

## Run locally

Python 3 is required.

From the project directory, start the server with a temporary article password:

```bash
ARTICLE_PASSWORD="choose-a-test-password" python3 server.py
```

If `ARTICLE_PASSWORD` is omitted, the server asks for a password in the terminal.

Then open:

- Front page: http://127.0.0.1:2121/
- Newsroom editor: http://127.0.0.1:2121/upload-news.html

Do not open the HTML files directly; the frontend uses API requests that require the Python server.

To use another interface or port:

```bash
python3 server.py --host 0.0.0.0 --port 8080
```

## Project structure

```text
.
├── index.html          # Front page and article reader
├── upload-news.html    # Newsroom article editor
├── server.py           # HTTP server and article API
├── data/
│   └── news_articles.sqlite3
└── assets/
    └── journal-mark.png
```

## Article editing

The newsroom editor supports:

- Article title, category, byline, summary, and body
- JPG, PNG, GIF, and WebP cover images up to 2 MB
- Editing existing articles
- Removing articles

Article mutations require the password configured by `ARTICLE_PASSWORD` or entered when the server starts.

## API

The server provides the following article endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/articles` | List articles |
| `POST` | `/api/articles` | Create an article |
| `PUT` | `/api/articles/:id` | Update an article |
| `DELETE` | `/api/articles/:id` | Remove an article |

Article changes require the `X-Article-Password` header.
