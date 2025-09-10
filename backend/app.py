# backend/app.py
import os
import uuid
import time
from datetime import datetime, timezone
from typing import List, Dict
from decimal import Decimal

from flask import Flask, request, jsonify
from flask_cors import CORS
import boto3
from botocore.exceptions import ClientError

# ==== 設定 ====
# 例: "http://localhost:5500,https://dvj7er4qsb0m.cloudfront.net"
ALLOWED = [
    s.strip()
    for s in os.environ.get("ALLOWED_ORIGINS", "http://localhost:5500").split(",")
    if s.strip()
]
TABLE_NAME = os.environ.get("TABLE_NAME", "todo")

# SES（使わないなら空でOK）
EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_TO = os.environ.get("EMAIL_TO")
SES_REGION = os.environ.get("SES_REGION", "ap-northeast-3")
NOTIFY_INTERVAL_HOURS = int(os.environ.get("NOTIFY_INTERVAL_HOURS", "0"))

# ==== AWS ====
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
ses = boto3.client("ses", region_name=SES_REGION)


def _to_jsonable(v):
    if isinstance(v, list):
        return [_to_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _to_jsonable(val) for k, val in v.items()}
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


# ==== Flask ====
app = Flask(__name__)
CORS(
    app,
    resources={r"/*": {"origins": ALLOWED}},
    methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ---- helpers ----
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_overdue(due_str: str, ref: datetime) -> bool:
    try:
        return parse_iso(due_str).astimezone(timezone.utc) < ref
    except Exception:
        return False


def current_user_id() -> str:
    """
    handler.py が JWT の sub を X-User-Sub に入れて渡してくる。
    万一無い場合は開発用に環境変数 USER_ID（既定 'me'）へフォールバック。
    """
    return request.headers.get("X-User-Sub") or os.environ.get("USER_ID", "me")


# ---------- Health ----------
@app.get("/health")
def health():
    return jsonify({"ok": True, "time": now_iso()})


# ---------- List ----------
@app.get("/tasks")
def list_tasks():
    user_id = current_user_id()
    resp = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("user_id").eq(
            current_user_id()
        ),
        ConsistentRead=True,
    )
    items = resp.get("Items", [])
    items = sorted(items, key=lambda x: float(x.get("sort", 1e18)))
    return jsonify(_to_jsonable(items))  # ← ここを変える


# ---------- Create ----------
@app.post("/tasks")
def create_task():
    user_id = current_user_id()
    body = request.get_json(force=True) or {}
    task_id = str(uuid.uuid4())
    item = {
        "user_id": user_id,
        "task_id": task_id,
        "title": body.get("title", ""),
        "status": body.get("status", "open"),
        "updated_at": now_iso(),
        "sort": int(time.time() * 1000),
    }
    if body.get("details"):
        item["details"] = body["details"]
    if body.get("due_date"):
        item["due_date"] = body["due_date"]
    if body.get("parent_id"):
        item["parent_id"] = body["parent_id"]

    table.put_item(Item=item)
    return jsonify(item), 201


# 共通：UpdateExpression を作る（SET/REMOVE 混在対応）
def _update_spec_from_payload(payload: dict):
    set_expr, remove_expr, names, values = [], [], {}, {}

    def add_set(k, v):
        if k == "sort" and v is not None:
            v = Decimal(str(v))
        set_expr.append(f"#_{k} = :{k}")
        names[f"#_{k}"] = k
        values[f":{k}"] = v

    for k in ("title", "status", "due_date", "parent_id", "details", "sort"):
        if k in payload:
            if k in ("due_date", "parent_id", "details") and payload[k] in (None, ""):
                remove_expr.append(f"#_{k}")
                names[f"#_{k}"] = k
            else:
                add_set(k, payload[k])

    if not set_expr and not remove_expr:
        return None

    add_set("updated_at", now_iso())

    parts = []
    if set_expr:
        parts.append("SET " + ", ".join(set_expr))
    if remove_expr:
        parts.append("REMOVE " + ", ".join(remove_expr))

    return {
        "UpdateExpression": " ".join(parts),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }


# ---------- Update ----------
@app.patch("/tasks/<task_id>")
def update_task(task_id):
    user_id = current_user_id()
    body = request.get_json(force=True) or {}
    spec = _update_spec_from_payload(body)
    if not spec:
        return jsonify({"message": "no fields"}), 400

    try:
        resp = table.update_item(
            Key={"user_id": user_id, "task_id": task_id},
            ReturnValues="ALL_NEW",
            **spec,
        )
        return jsonify(_to_jsonable(resp["Attributes"]))  # ← ここを変える
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return jsonify({"message": "not found"}), 404
        raise


# ---------- Delete ----------
@app.delete("/tasks/<task_id>")
def delete_task(task_id):
    user_id = current_user_id()
    table.delete_item(Key={"user_id": user_id, "task_id": task_id})
    return "", 204


# ---------- Bulk ----------
@app.post("/tasks/bulk")
def bulk_tasks():
    user_id = current_user_id()
    body = request.get_json(force=True) or {}
    ops = body.get("ops", [])
    if not isinstance(ops, list):
        return jsonify({"message": "ops must be an array"}), 400
    if len(ops) > 50:
        return jsonify({"message": "too many ops (<=50)"}), 400

    results, errors = [], []
    for i, op in enumerate(ops):
        tid = op.get("id")
        act = (op.get("action") or "").lower()
        try:
            if not tid or act not in ("delete", "patch", "update", "status"):
                raise ValueError("invalid op")

            if act == "delete":
                table.delete_item(Key={"user_id": user_id, "task_id": tid})
                results.append({"i": i, "id": tid, "action": "delete", "ok": True})
                continue

            payload = op.get("payload") or {}
            if act == "status" and isinstance(payload, str):
                payload = {"status": payload}

            spec = _update_spec_from_payload(payload)
            if not spec:
                raise ValueError("no fields")

            table.update_item(
                Key={"user_id": user_id, "task_id": tid},
                ReturnValues="NONE",
                **spec,
            )
            results.append({"i": i, "id": tid, "action": "patch", "ok": True})
        except Exception as e:
            errors.append({"i": i, "id": tid, "action": act, "error": str(e)})

    return jsonify({"ok": len(errors) == 0, "results": results, "errors": errors})


# ---------- Overdue Mail Notify ----------
def _collect_overdue_targets(items: List[Dict]) -> List[Dict]:
    now = datetime.now(timezone.utc)
    targets = []
    for it in items:
        due = it.get("due_date")
        if not due:
            continue
        if it.get("status") not in ("open", "overdue"):
            continue
        if not is_overdue(due, now):
            continue
        targets.append(it)
    return targets


def _send_overdue_email(targets: List[Dict]) -> None:
    if not EMAIL_FROM or not EMAIL_TO:
        raise RuntimeError("EMAIL_FROM/EMAIL_TO environment variables are required.")
    lines = [
        f"- {t.get('title','(no title)')} (due: {t.get('due_date','-')})"
        for t in targets
    ]
    body_text = "以下のタスクが期限超過です：\n\n" + "\n".join(lines)
    ses.send_email(
        Source=EMAIL_FROM,
        Destination={"ToAddresses": [EMAIL_TO]},
        Message={
            "Subject": {"Data": "【ToDo】期限超過タスクの通知"},
            "Body": {"Text": {"Data": body_text}},
        },
        ReplyToAddresses=[EMAIL_FROM],
    )


def _mark_notified(user_id: str, targets: List[Dict]) -> int:
    now_s = now_iso()
    cnt = 0
    for it in targets:
        try:
            table.update_item(
                Key={"user_id": user_id, "task_id": it["task_id"]},
                UpdateExpression="SET #s=:s, #n=:n, #u=:u",
                ExpressionAttributeNames={
                    "#s": "status",
                    "#n": "overdue_notified_at",
                    "#u": "updated_at",
                },
                ExpressionAttributeValues={":s": "overdue", ":n": now_s, ":u": now_s},
            )
            cnt += 1
        except Exception:
            pass
    return cnt


@app.post("/notify/overdue")
def notify_overdue():
    user_id = current_user_id()
    resp = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("user_id").eq(user_id)
    )
    items = resp.get("Items", [])
    targets = _collect_overdue_targets(items)
    if not targets:
        return jsonify({"sent": 0, "notified": 0, "message": "no overdue tasks"}), 200
    try:
        _send_overdue_email(targets)
    except Exception as e:
        return jsonify({"sent": 0, "error": str(e)}), 500
    updated = _mark_notified(user_id, targets)
    return jsonify({"sent": 1, "target_count": len(targets), "notified": updated}), 200


# プリフライト
@app.route("/", methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def cors_preflight(path=None):
    return ("", 204)
