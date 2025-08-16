# backend/app.py
import os
import json
import uuid
from datetime import datetime, timezone
from flask import Flask, request, jsonify
import boto3
from botocore.exceptions import ClientError
from flask_cors import CORS

CORS(app)

TABLE_NAME = os.environ.get("TABLE_NAME", "todo")
USER_ID = os.environ.get("USER_ID", "me")  # 個人利用なので固定

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

app = Flask(__name__)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


@app.get("/health")
def health():
    return jsonify({"ok": True, "time": now_iso()})


@app.get("/tasks")
def list_tasks():
    # 期限順に並べたいなら後でGSIを追加。まずは単純Query。
    resp = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("user_id").eq(USER_ID)
    )
    return jsonify(resp.get("Items", []))


@app.post("/tasks")
def create_task():
    body = request.get_json(force=True) or {}
    task_id = str(uuid.uuid4())
    item = {
        "user_id": USER_ID,
        "task_id": task_id,
        "title": body.get("title", ""),
        "status": body.get("status", "open"),
        "due_date": body.get("due_date", None),
        "updated_at": now_iso(),
    }
    table.put_item(Item=item)
    return jsonify(item), 201


@app.patch("/tasks/<task_id>")
def update_task(task_id):
    body = request.get_json(force=True) or {}
    expr = []
    names = {}
    values = {}
    for k in ["title", "status", "due_date"]:
        if k in body:
            expr.append(f"#_{k} = :{k}")
            names[f"#_{k}"] = k
            values[f":{k}"] = body[k]
    if not expr:
        return jsonify({"message": "no fields"}), 400
    expr.append("#_updated_at = :updated_at")
    names["#_updated_at"] = "updated_at"
    values[":updated_at"] = now_iso()

    try:
        resp = table.update_item(
            Key={"user_id": USER_ID, "task_id": task_id},
            UpdateExpression="SET " + ", ".join(expr),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        return jsonify(resp["Attributes"])
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return jsonify({"message": "not found"}), 404
        raise


@app.delete("/tasks/<task_id>")
def delete_task(task_id):
    table.delete_item(Key={"user_id": USER_ID, "task_id": task_id})
    return "", 204
