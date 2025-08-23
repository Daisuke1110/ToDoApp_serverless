import json, urllib.request

API_URL = "https://wqwkh06vkl.execute-api.ap-northeast-3.amazonaws.com/notify/overdue"  # ? ????API???????

def lambda_handler(event, context):
    # HTTP?????????POST????????? Content-Type
    req = urllib.request.Request(API_URL, method="POST",
                                 headers={"Content-Type":"application/json"})
    # ??????????
    with urllib.request.urlopen(req) as resp:
        body = resp.read().decode()
        # ?????????EventBridge?????
        return {"status": resp.status, "body": body}
