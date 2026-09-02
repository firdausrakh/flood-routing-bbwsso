import os

bind = f"0.0.0.0:{os.environ.get('PORT', '80')}"
workers = 1
worker_class = "uvicorn_worker.UvicornWorker"
timeout = 180
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
capture_output = True
preload_app = False
