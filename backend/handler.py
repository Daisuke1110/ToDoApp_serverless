# backend/handler.py
from app import app
import serverless_wsgi


def handler(event, context):
    """
    - API Gateway HTTP API(v2) / REST API どちらでもOK
    - JWTのsub/emailをヘッダに積んで Flask 側で拾えるようにする
    """
    claims = {}
    rc = event.get("requestContext", {}) or {}
    authorizer = rc.get("authorizer", {}) or {}

    # HTTP API(v2)
    if isinstance(authorizer, dict) and "jwt" in authorizer:
        claims = (authorizer.get("jwt") or {}).get("claims", {}) or {}
    # REST API(v1) の一般的パターン
    elif isinstance(authorizer, dict) and "claims" in authorizer:
        claims = authorizer.get("claims") or {}

    sub = claims.get("sub")
    email = claims.get("email") or claims.get("cognito:username")

    # Flask から参照しやすいよう、カスタムヘッダに詰める
    headers = event.get("headers") or {}
    if sub:
        headers["X-User-Sub"] = sub
    if email:
        headers["X-User-Email"] = email
    event["headers"] = headers

    # ★ awsgi は使わない！ serverless-wsgi でハンドリング
    return serverless_wsgi.handle_request(app, event, context)
