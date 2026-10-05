# Bitmail

Bitmail is a high-performance, self-hosted mass email dispatch and subscriber management system. Built with Python, FastAPI, and an asynchronous architecture, Bitmail provides immediate direct dispatch, configurable rate-governed queueing, multi-group customer management, dynamic template authoring, and complete local EML vault storage.

## Features

- High Throughput Asynchronous Dispatch: Asynchronous queue-based sending engine capable of handling high-volume broadcasts with configurable rate limits.
- Configurable Speed Governors: Supports preset throughputs (10, 25, 50, 100 emails/sec) as well as custom user-defined rates to prevent ISP throttling.
- Customer and Group Management: Manage audiences with multi-list support, bulk email pasting, and CSV import with automatic schema and custom attribute detection.
- Dynamic Merge Variables: Built-in tags for personalizing subject lines and message bodies (first_name, email, company, unsubscribe_url) plus custom dynamic placeholders discovered from imported datasets.
- Interactive Template Studio: Visual email template designer featuring responsive preview inspection for desktop and mobile viewports.
- Storage Vault: Comprehensive local archiving of all outgoing messages in standard EML format with raw MIME header viewer and download capability.
- Multi-Relay SMTP Configuration: Connect and manage multiple SMTP endpoints with individual credential management and health testing.
- Real-Time Live Monitoring: Live dispatch console with progress tracking, active throughput metrics, and completion state indicators.

## System Architecture

Bitmail is structured as a modular asynchronous web application:

- Backend: FastAPI (Python 3.10+) running on ASGI/Uvicorn.
- Database: SQLite with asynchronous access (aiosqlite) and optimized indexing.
- Delivery Engine: Python asyncio SMTP worker with token-bucket rate limiting.
- Frontend: Responsive single-page application utilizing TailwindCSS design tokens and vanilla JavaScript logic.
- Storage: Local filesystem EML vault with database-indexed metadata.

## Directory Structure

```
mass-email-system/
|-- app/
|   |-- main.py              # Application entry point and router registration
|   |-- config.py            # Global settings and environment configuration
|   |-- db.py                # Database connection, schemas, and migrations
|   |-- models.py            # Pydantic schemas for validation and API contracts
|   |-- email_engine.py      # Asynchronous SMTP dispatch engine and worker pool
|   |-- routes/
|   |   |-- analytics.py     # Metrics, open/click telemetry, and stats endpoints
|   |   |-- campaigns.py     # Campaign creation, scheduling, and quick broadcast
|   |   |-- pages.py         # Primary dashboard page views
|   |   |-- smtp.py          # SMTP relay configuration and connection tests
|   |   |-- storage.py       # EML vault retrieval and inspection endpoints
|   |   |-- subscribers.py   # Customer CRUD, group management, and CSV ingestion
|   |   `-- templates.py     # Template Studio persistence and presets
|   |-- static/
|   |   |-- css/style.css    # High-contrast theme styling
|   |   `-- js/app.js        # Client-side state, modal workflows, and API client
|   `-- templates/
|       `-- index.html       # Primary UI template
|-- tests/                   # Automated unit and integration test suite
|-- requirements.txt         # Python dependencies
`-- README.md
```

## Quick Start

### Prerequisites

- Python 3.10 or higher
- pip (Python package installer)
- Git

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/zonicisalive/bitmail.git
   cd bitmail
   ```

2. Create and activate a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. Install required dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Launch the application server:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```

5. Access the web interface:
   Open your browser and navigate to `http://localhost:8000`.

## Configuration

### Mail Server Setup

To configure an outgoing mail server:
1. Navigate to the Mail Servers section in the sidebar.
2. Click Add Mail Server.
3. Provide your SMTP host, port, username, password, and encryption mode (TLS, STARTTLS, or Plain).
4. Run the Test Connection utility to verify deliverability before dispatching broadcasts.

### Managing Customer Groups

1. Navigate to the Customers & Subscriber Lists section.
2. Click the + New Group button.
3. Enter a group name (e.g. VIP Customers) and optional description.
4. Import contacts via CSV or bulk paste and assign them to the desired group.

## Running Tests

Execute the automated test suite with Python's unittest module:

```bash
python -m unittest discover tests
```

## License

MIT License. See LICENSE file for details.
