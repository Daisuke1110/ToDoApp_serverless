# backend/duecheck.py
import os
from datetime import datetime, timezone
import boto3

TABLE_NAME = os.environ.get("TABLE_NAME", "todo")
USER_ID = os.environ.get("USER_ID", "me")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def handler(event, context):
    # 例：期限切れを"open"→"overdue"にマーキング（簡略版）
    now = datetime.now(timezone.utc).isoformat()
    resp = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("user_id").eq(USER_ID)
    )
    items = resp.get("Items", [])
    for it in items:
        due = it.get("due_date")
        if it.get("status") == "open" and due and due < now:
            table.update_item(
                Key={"user_id": USER_ID, "task_id": it["task_id"]},
                UpdateExpression="SET #s = :s, #u = :u",
                ExpressionAttributeNames={"#s": "status", "#u": "updated_at"},
                ExpressionAttributeValues={":s": "overdue", ":u": now},
            )
    return {"updated": True, "count": len(items)}
