# backend/app.py
import os
import uuid
from datetime import datetime, timezone
from typing import List, Dict

from flask import Flask, request, jsonify
from flask_cors import CORS
import boto3
from botocore.exceptions import ClientError

# ==== 設定 ====
ALLOWED = os.environ.get("ALLOWED_ORIGINS", "http://localhost:5500").split(",")
TABLE_NAME = os.environ.get("TABLE_NAME", "todo")
USER_ID = os.environ.get("USER_ID", "me")  # 個人利用なので固定

# 追加: SES設定（送信元/宛先は環境変数に）
EMAIL_FROM = os.environ.get("EMAIL_FROM")  # 例: no-reply@example.com（SESで検証済み）
EMAIL_TO = os.environ.get(
    "EMAIL_TO"
)  # 例: you@example.com（サンドボックス中は受信側も検証）
SES_REGION = os.environ.get("SES_REGION", "ap-northeast-3")

# ==== AWS ====
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
ses = boto3.client("ses", region_name=SES_REGION)

# ==== Flask ====
app = Flask(__name__)
# CORS はここ「だけ」で設定（後段の after_request は不要）
CORS(
    app,
    resources={r"/*": {"origins": ALLOWED}},
    methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    """ISO 8601文字列をdatetimeへ。'Z'にも対応。"""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_overdue(due_str: str, ref: datetime) -> bool:
    try:
        return parse_iso(due_str).astimezone(timezone.utc) < ref
    except Exception:
        # 形式不正は期限判定不可としてFalse扱い（ログに出したければここでprintなど）
        return False


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


# ---------- Overdue Mail Notify ----------
def _collect_overdue_targets(items: List[Dict]) -> List[Dict]:
    """statusがopen/overdue かつ due_dateが過去、かつ未通知のものを抽出"""
    now = datetime.now(timezone.utc)
    targets = []
    for it in items:
        due = it.get("due_date")
        if not due:
            continue
        if it.get("status") not in ("open", "overdue"):
            continue
        if it.get("overdue_notified_at"):  # 既に通知済み
            continue
        if is_overdue(due, now):
            targets.append(it)
    return targets


def _send_overdue_email(targets: List[Dict]) -> None:
    """Amazon SESで1通にまとめて送信"""
    if not EMAIL_FROM or not EMAIL_TO:
        # 必須設定がない場合は送信せずに例外
        raise RuntimeError("EMAIL_FROM/EMAIL_TO environment variables are required.")

    lines = []
    for t in targets:
        title = t.get("title", "(no title)")
        due = t.get("due_date", "-")
        lines.append(f"- {title} (due: {due})")

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


def _mark_notified(targets: List[Dict]) -> int:
    """通知済みフラグとstatus=overdueを付与"""
    now_s = now_iso()
    cnt = 0
    for it in targets:
        try:
            table.update_item(
                Key={"user_id": USER_ID, "task_id": it["task_id"]},
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
            # 個別失敗はスキップ（必要ならCloudWatchに出力）
            pass
    return cnt


@app.post("/notify/overdue")
def notify_overdue():
    """
    期限超過タスクをまとめてメール通知し、通知済みフラグを立てる。
    - 手動トリガー用のHTTPエンドポイント。
    - 本番はEventBridge SchedulerでこのURLを叩くか、別Lambdaでduecheckと同様の処理を実行する運用を推奨。
    """
    # 全タスク取得（規模が大きくなればGSIでdue_date範囲クエリへ移行）
    resp = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("user_id").eq(USER_ID)
    )
    items = resp.get("Items", [])

    targets = _collect_overdue_targets(items)
    if not targets:
        return jsonify({"sent": 0, "notified": 0, "message": "no overdue tasks"}), 200

    try:
        _send_overdue_email(targets)
    except Exception as e:
        return jsonify({"sent": 0, "error": str(e)}), 500

    updated = _mark_notified(targets)
    return jsonify({"sent": 1, "target_count": len(targets), "notified": updated}), 200
