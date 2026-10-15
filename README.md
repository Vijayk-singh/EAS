# Employee Attendance & Analytics API

FastAPI and MongoDB backend for an employee attendance management and analytics service, implementing the complete contract, concurrency requirements, and business rules (R1–R10).

## Tech Stack
- **Python:** 3.11+
- **Framework:** FastAPI / Pydantic v2
- **Database:** MongoDB 6.0+ (Atlas or local), PyMongo
- **Server:** Uvicorn

## Setup & Running

### 1. Environment & Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configuration
The application reads configuration from environment variables (with optional `.env` fallback):
- `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`)
- `MONGO_DB`: Database name (default: `attendance_db`)

Process environment variables take precedence over `.env`.

### 3. Start the Server
```bash
uvicorn app.main:app --port 8000
```
All required indexes are created automatically and idempotently on startup.

### 4. Interactive Documentation
- **Swagger UI:** `http://localhost:8000/docs`
- **ReDoc:** `http://localhost:8000/redoc`
- **Health Check:** `http://localhost:8000/health`

## Project Structure
- `app/main.py`: Complete application code including Pydantic models, API routes, business logic (R1–R10), and MongoDB aggregation pipelines.
- `requirements.txt`: Core Python dependencies.
- `REVIEW.md`: Detailed review of starter defects, failing symptoms, and fixes applied.
- `DECISIONS.md`: Design answers covering database indexing, concurrency safety, competition ranking ties, headcount logic, and scalability.
