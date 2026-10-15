"""Employee Attendance & Analytics API."""
import calendar
import os
from datetime import date as Date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC, IST = timezone.utc, timezone(timedelta(hours=5, minutes=30))
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=1500)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}
MIN_MS, MAX_MS = 100000000000, 4102444800000

def error(message: str): raise HTTPException(422, message)
def as_date(s: str) -> Date:
    try: value = datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError): error("date must be YYYY-MM-DD")
    if value.isoformat() != s: error("date must be YYYY-MM-DD")
    return value
def month_range(s: str) -> tuple[Date, Date]:
    try: start = datetime.strptime(s, "%Y-%m").date().replace(day=1)
    except (TypeError, ValueError): error("month must be YYYY-MM")
    if len(s) != 7 or start.strftime("%Y-%m") != s: error("month must be YYYY-MM")
    return start, start.replace(day=calendar.monthrange(start.year, start.month)[1])
def ms(v: Any) -> int:
    if type(v) is not int or not MIN_MS <= v <= MAX_MS: error("timestamp must be an epoch-millisecond integer")
    return v
def to_dt(v: int) -> datetime: return datetime.fromtimestamp((ms(v) // 1000), UTC)
def now() -> datetime: return datetime.now(UTC).replace(microsecond=0)
def utc(v: datetime) -> datetime: return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)
def to_ms(v: Optional[datetime]) -> Optional[int]: return None if v is None else int(utc(v).timestamp() * 1000)
def rounded(v: Any, n: int) -> float: return float(Decimal(str(v)).quantize(Decimal("1").scaleb(-n), rounding=ROUND_HALF_UP))
def clock(s: str) -> tuple[int, int]:
    try:
        h, m = map(int, s.split(":")); assert 0 <= h < 24 and 0 <= m < 60
        return h, m
    except (AttributeError, ValueError, AssertionError): error("time must be HH:MM")
def shift(day: str, time: str) -> datetime:
    h, m = clock(time)
    return datetime.combine(as_date(day), datetime.min.time(), IST).replace(hour=h, minute=m)
def attendance_day(pin: datetime, emp: dict) -> str:
    local = utc(pin).astimezone(IST); day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.time() < datetime.strptime(emp["shift_end"], "%H:%M").time(): day -= timedelta(days=1)
    return day.isoformat()
def late(pin: datetime, start: str, day: str) -> int:
    secs = (utc(pin).astimezone(IST) - shift(day, start)).total_seconds()
    return int(secs // 60) if secs > 600 else 0
def derived(emp: dict, rec: dict) -> dict:
    status, pin, pout = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    if status not in PRESENT: return {"punch_in": None, "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if pin is None: error("a presence status requires punch_in")
    out = {"punch_in": pin, "punch_out": pout, "late_minutes": late(pin, emp["shift_start"], rec["date"])}
    if pout is None: return out | {"work_hours": None, "overtime_minutes": 0, "half_day": False}
    seconds = (utc(pout) - utc(pin)).total_seconds()
    if not 0 < seconds <= 86400: error("punch_out must be after punch_in and within 24 hours")
    hours = rounded(Decimal(str(seconds)) / Decimal("3600"), 2)
    end = shift(rec["date"], emp["shift_end"])
    if emp["shift_end"] <= emp["shift_start"]: end += timedelta(days=1)
    overtime = int((utc(pout).astimezone(IST) - end).total_seconds() // 60)
    return out | {"work_hours": hours, "overtime_minutes": overtime if overtime >= 30 else 0, "half_day": hours < 4.5}
def json_value(v: Any) -> Any:
    if isinstance(v, datetime): return to_ms(v)
    if isinstance(v, list): return [json_value(x) for x in v]
    if isinstance(v, dict): return {k: json_value(x) for k, x in v.items() if k != "_id"}
    return v
def attendance_json(doc: dict) -> dict:
    out = json_value(doc)
    for k, v in (("history", []), ("half_day", False), ("late_minutes", 0), ("overtime_minutes", 0), ("work_hours", None), ("punch_out", None)): out.setdefault(k, v)
    return out

indexes_ready = False
def ensure_indexes():
    global indexes_ready
    if not indexes_ready:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_employee_code")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
        db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_attendance_day")
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_listing")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="employee_month")
        db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)], name="date_employee")
        indexes_ready = True

@app.on_event("startup")
def create_indexes():
    try: ensure_indexes()
    except PyMongoError: pass

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$"); name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"); department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"); shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$"); joined_on: str
    @field_validator("joined_on")
    @classmethod
    def valid_date(cls, v): as_date(v); return v
    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self
class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore"); emp_code: str; punched_at: Optional[StrictInt] = None; status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_ms(cls, v):
        if v is None: raise ValueError("punched_at cannot be null")
        return ms(v)
class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore"); emp_code: str; punched_at: Optional[StrictInt] = None
    @field_validator("punched_at")
    @classmethod
    def valid_ms(cls, v):
        if v is None: raise ValueError("punched_at cannot be null")
        return ms(v)
class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None; punch_in: Optional[StrictInt] = None; punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200); regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("status", "punch_in", "punch_out")
    @classmethod
    def not_none_if_supplied(cls, v):
        if v is None: raise ValueError("field cannot be null")
        return v
    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ms(cls, v): return ms(v)

@app.get("/health")
def health():
    try:
        db.command("ping")
        ensure_indexes()
    except PyMongoError: raise HTTPException(503, "MongoDB unavailable or indexes not ready")
    return {"status": "ok"}
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump() | {"created_at": now()}
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return json_value(doc)
@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=100)] = 20):
    q = {"department": department} if department is not None else {}; total = db.employees.count_documents(q)
    return {"items": [json_value(x) for x in db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page-1)*page_size).limit(page_size)], "total": total, "page": page, "page_size": page_size}
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    pin = to_dt(body.punched_at) if body.punched_at is not None else now(); day = attendance_day(pin, emp)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": pin, "punch_out": None, "work_hours": None, "late_minutes": late(pin, emp["shift_start"], day), "overtime_minutes": 0, "half_day": False, "history": []}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "already punched in for this date")
    return attendance_json(doc)
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    pout = to_dt(body.punched_at) if body.punched_at is not None else now()
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": pout}}, sort=[("punch_in", -1)])
    if not rec: raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None: raise HTTPException(409, "already punched out")
    changed = db.attendance_logs.find_one_and_update({"_id": rec["_id"], "punch_out": None}, {"$set": derived(emp, rec | {"punch_out": pout})}, return_document=ReturnDocument.AFTER)
    if changed is None: raise HTTPException(409, "already punched out")
    return attendance_json(changed)
@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None, page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=100)] = 20):
    if date_from: as_date(date_from)
    if date_to: as_date(date_to)
    if date_from and date_to and date_from > date_to: error("date_from must not be after date_to")
    q = ({"emp_code": emp_code} if emp_code else {}) | ({"status": status} if status else {})
    if date_from or date_to: q["date"] = ({"$gte": date_from} if date_from else {}) | ({"$lte": date_to} if date_to else {})
    total = db.attendance_logs.count_documents(q); docs = db.attendance_logs.find(q, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip((page-1)*page_size).limit(page_size)
    return {"items": [attendance_json(x) for x in docs], "total": total, "page": page, "page_size": page_size}
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    as_date(date); emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not current: raise HTTPException(404, "attendance record not found")
    fields = body.model_fields_set
    if body.status in {"ABSENT", "LEAVE"} and ({"punch_in", "punch_out"} & fields): error("ABSENT and LEAVE cannot include punch times")
    candidate = current.copy()
    if "status" in fields: candidate["status"] = body.status
    if "punch_in" in fields: candidate["punch_in"] = to_dt(body.punch_in)
    if "punch_out" in fields: candidate["punch_out"] = to_dt(body.punch_out)
    if candidate["status"] in {"ABSENT", "LEAVE"}: candidate["punch_in"] = candidate["punch_out"] = None
    if candidate.get("punch_in") and attendance_day(candidate["punch_in"], emp) != date: error("punch_in must belong to the record date")
    values = derived(emp, candidate); changes = {}
    defaults = {"half_day": False, "late_minutes": 0, "overtime_minutes": 0}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        old, new = current.get(key, defaults.get(key)), values.get(key, candidate.get(key))
        if old != new: changes[key] = {"from": old, "to": new}
    if not changes: error("regularization changes nothing")
    race = {"history": current["history"]} if "history" in current else {"history": {"$exists": False}}
    event = {"at": now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    changed = db.attendance_logs.find_one_and_update({"_id": current["_id"]} | race, {"$set": {"status": candidate["status"]} | values, "$push": {"history": event}}, return_document=ReturnDocument.AFTER)
    if not changed: raise HTTPException(409, "attendance record changed concurrently")
    return attendance_json(changed)

def monthly_pipeline(code, start, end):
    return [{"$match": {"emp_code": code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}}, {"$set": {"wd": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}}, {"$group": {"_id": None,
        "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT)]}, {"$gte": ["$wd", 2]}, {"$lte": ["$wd", 6]}]}, {"$cond": [{"$ifNull": ["$half_day", False]}, .5, 1]}, 0]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}]
def weekday_count(start, end, joined):
    start = max(start, as_date(joined))
    return 0 if start > end else sum((start + timedelta(days=i)).weekday() < 5 for i in range((end-start).days+1))
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = month_range(month); emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    total = next(iter(db.attendance_logs.aggregate(monthly_pipeline(emp_code, start, end))), {}); days = weekday_count(start, end, emp["joined_on"]); present = total.get("present_days", 0)
    return {"emp_code": emp_code, "month": month, "working_days": days, "present_days": present, "leave_days": total.get("leave_days", 0), "late_count": total.get("late_count", 0), "total_late_minutes": total.get("total_late_minutes", 0), "total_overtime_minutes": total.get("total_overtime_minutes", 0), "attendance_pct": rounded(Decimal(str(present))*100/days, 4) if days else None}

def summary_pipeline(start, end, department):
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None: match["department"] = department
    return [{"$match": match}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}, {"$set": {"wd": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}}], "as": "logs"}}, {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}}, {"$group": {"_id": "$department", "people": {"$addToSet": "$emp_code"},
        "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$gte": ["$logs.wd", 2]}, {"$lte": ["$logs.wd", 6]}]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, .5, 1]}, 0]}},
        "works": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", "$$REMOVE"]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}}, "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}}, "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}}, {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$people"}, "present_days": 1, "avg_work_hours": {"$cond": [{"$gt": [{"$size": "$works"}, 0]}, {"$avg": "$works"}, None]}, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}, {"$sort": {"department": 1}}]
@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = month_range(month); items = list(db.employees.aggregate(summary_pipeline(start, end, department)))
    for item in items: item["avg_work_hours"] = rounded(item["avg_work_hours"], 2) if item["avg_work_hours"] is not None else None
    return {"month": month, "items": items}

def leaderboard_pipeline(start, end, department, limit):
    employee_match = {"employee.department": department} if department is not None else {}
    return [{"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}}, {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}, {"$match": employee_match}, {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}}, {"$match": {"rank": {"$lte": limit}}}, {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}, {"$sort": {"total_late_minutes": -1, "emp_code": 1}}]
@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    start, end = month_range(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(start, end, department, limit)))}

def trend_pipeline(department, start, end):
    days = (end-start).days
    # Database-side generated series guarantees a row for every requested day.
    return [{"$documents": [{"start": datetime.combine(start, datetime.min.time(), UTC), "days": days, "department": department}]}, {"$project": {"department": 1, "date": {"$map": {"input": {"$range": [0, {"$add": ["$days", 1]}]}, "as": "n", "in": {"$dateToString": {"format": "%Y-%m-%d", "date": {"$dateAdd": {"startDate": "$start", "unit": "day", "amount": "$$n"}}}}}}}}, {"$unwind": "$date"}, {"$lookup": {"from": "employees", "let": {"day": "$date", "dept": "$department"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", "$$dept"]}, {"$lte": ["$joined_on", "$$day"]}]}}}], "as": "employees"}}, {"$unwind": {"path": "$employees", "preserveNullAndEmptyArrays": True}}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$employees.emp_code", "day": "$date"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$eq": ["$date", "$$day"]}]}}}], "as": "log"}}, {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}}, {"$group": {"_id": "$date", "headcount": {"$sum": {"$cond": [{"$ne": ["$employees", None]}, 1, 0]}}, "present_count": {"$sum": {"$cond": [{"$in": ["$log.status", list(PRESENT)]}, {"$cond": [{"$ifNull": ["$log.half_day", False]}, .5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]}, 1, 0]}}}}, {"$set": {"date": "$_id", "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$_id"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$_id"}}}, 6]}]}}}, {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}}, {"$sort": {"date": 1}}, {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}}, {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}}]
@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[str, Query(alias="from")], to: str):
    start, end = as_date(from_), as_date(to)
    if end < start: error("to must not be before from")
    if (end-start).days + 1 > 92: error("range may not exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}): raise HTTPException(404, "department not found")
    rows = db.command("aggregate", 1, pipeline=trend_pipeline(department, start, end), cursor={})["cursor"]["firstBatch"]
    for row in rows:
        row["attendance_rate"] = rounded(row["attendance_rate"], 4) if row["attendance_rate"] is not None else None
        row["moving_avg_7d"] = rounded(row["moving_avg_7d"], 4) if row["moving_avg_7d"] is not None else None
    return {"department": department, "items": rows}

@app.get("/admin/explain/{endpoint}")
def explain(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"], emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None, limit: Annotated[int, Query(ge=1, le=50)] = 10, date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None, page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=100)] = 20, from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None):
    if endpoint == "attendance_list":
        if date_from: as_date(date_from)
        if date_to: as_date(date_to)
        if date_from and date_to and date_from > date_to: error("date_from must not be after date_to")
        q = ({"emp_code": emp_code} if emp_code else {}) | ({"status": status} if status else {})
        if date_from or date_to: q["date"] = ({"$gte": date_from} if date_from else {}) | ({"$lte": date_to} if date_to else {})
        command, collection = {"explain": {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}, "verbosity": "executionStats"}, "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: error("emp_code and month are required")
        start, end = month_range(month); command, collection = {"explain": {"aggregate": "attendance_logs", "pipeline": monthly_pipeline(emp_code, start, end), "cursor": {}}, "verbosity": "executionStats"}, "attendance_logs"
    elif endpoint == "department_summary":
        if not month: error("month is required")
        start, end = month_range(month); command, collection = {"explain": {"aggregate": "employees", "pipeline": summary_pipeline(start, end, department), "cursor": {}}, "verbosity": "executionStats"}, "employees"
    elif endpoint == "late_leaderboard":
        if not month: error("month is required")
        start, end = month_range(month); command, collection = {"explain": {"aggregate": "attendance_logs", "pipeline": leaderboard_pipeline(start, end, department, limit), "cursor": {}}, "verbosity": "executionStats"}, "attendance_logs"
    else:
        if not department or not from_ or not to: error("department, from and to are required")
        start, end = as_date(from_), as_date(to)
        if end < start or (end-start).days + 1 > 92: error("invalid trend range")
        command, collection = {"explain": {"aggregate": 1, "pipeline": trend_pipeline(department, start, end), "cursor": {}}, "verbosity": "executionStats"}, "database"
    return {"endpoint": endpoint, "collection": collection, "explain": json_value(db.command(command))}
