# backend/app.py
import os
import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_cors import CORS
import boto3
from botocore.exceptions import ClientError

# ==== 設定 ====
ALLOWED = os.environ.get("ALLOWED_ORIGINS", "http://localhost:5500").split(",")
TABLE_NAME = os.environ.get("TABLE_NAME", "todo")
USER_ID = os.environ.get("USER_ID", "me")  # 個人利用なので固定

# ==== AWS ====
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

# ==== Flask ====
app = Flask(__name__)
# CORS はここ「だけ」で設定（後段の after_request は削除）
CORS(
    app,
    resources={r"/*": {"origins": ALLOWED}},
    methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- Health ----------
@app.get("/health")
def health():
    return jsonify({"ok": True, "time": now_iso()})


# ---------- List ----------
@app.get("/tasks")
def list_tasks():
    resp = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("user_id").eq(USER_ID)
    )
    return jsonify(resp.get("Items", []))


# ---------- Create ----------
@app.post("/tasks")
def create_task():
    body = request.get_json(force=True) or {}
    task_id = str(uuid.uuid4())
    item = {
        "user_id": USER_ID,
        "task_id": task_id,
        "title": body.get("title", ""),
        "status": body.get("status", "open"),
        "updated_at": now_iso(),
    }
    # None/空は保存しない（DynamoDB は None を受け付けない）
    due = body.get("due_date")
    if due:
        item["due_date"] = due

    table.put_item(Item=item)
    return jsonify(item), 201


# 共通：UpdateExpression を作る（SET/REMOVE 混在対応）
def _update_spec_from_payload(payload: dict):
    set_expr, remove_expr, names, values = [], [], {}, {}

    def add_set(k, v):
        set_expr.append(f"#_{k} = :{k}")
        names[f"#_{k}"] = k
        values[f":{k}"] = v

    for k in ("title", "status", "due_date"):
        if k in payload:
            if k == "due_date" and payload[k] in (None, ""):
                remove_expr.append("#_due_date")
                names["#_due_date"] = "due_date"
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
    body = request.get_json(force=True) or {}
    spec = _update_spec_from_payload(body)
    if not spec:
        return jsonify({"message": "no fields"}), 400

    try:
        resp = table.update_item(
            Key={"user_id": USER_ID, "task_id": task_id},
            ReturnValues="ALL_NEW",
            **spec,
        )
        return jsonify(resp["Attributes"])
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return jsonify({"message": "not found"}), 404
        raise


# ---------- Delete ----------
@app.delete("/tasks/<task_id>")
def delete_task(task_id):
    table.delete_item(Key={"user_id": USER_ID, "task_id": task_id})
    return "", 204


# ---------- Bulk ----------
@app.post("/tasks/bulk")
def bulk_tasks():
    """
    body:
    { "ops": [
        {"id":"<id>","action":"delete"},
        {"id":"<id>","action":"patch","payload":{"status":"done"}},
        {"id":"<id>","action":"status","payload":"open"},
        {"id":"<id>","action":"patch","payload":{"due_date":""}}  # due_date を削除
    ] }
    """
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
                table.delete_item(Key={"user_id": USER_ID, "task_id": tid})
                results.append({"i": i, "id": tid, "action": "delete", "ok": True})
                continue

            payload = op.get("payload") or {}
            if act == "status" and isinstance(payload, str):
                payload = {"status": payload}

            spec = _update_spec_from_payload(payload)
            if not spec:
                raise ValueError("no fields")

            table.update_item(
                Key={"user_id": USER_ID, "task_id": tid},
                ReturnValues="NONE",
                **spec,
            )
            results.append({"i": i, "id": tid, "action": "patch", "ok": True})
        except Exception as e:
            errors.append({"i": i, "id": tid, "action": act, "error": str(e)})

    return jsonify({"ok": len(errors) == 0, "results": results, "errors": errors})
