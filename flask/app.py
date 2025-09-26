from flask import Flask

app = Flask(__name__)
application = app  # for mod_wsgi / Apache

@app.get("/")
def root():
    return "ok\n", 200

# optional health check
@app.get("/healthz")
def healthz():
    return "ok\n", 200