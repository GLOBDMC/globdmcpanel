from flask import Flask

app = Flask(_name_)

@app.route("/")
def home():
    return "GlobDMC Panel Aktif"