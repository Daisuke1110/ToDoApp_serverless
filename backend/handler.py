# backend/handler.py
from apig_wsgi import make_lambda_handler
from app import app  # Flaskインスタンス

handler = make_lambda_handler(app)  # SAMのHandlerにこれを指定
