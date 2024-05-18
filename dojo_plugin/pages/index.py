from flask import Blueprint, render_template

index = Blueprint("pwncollege_index", __name__)

@index.route("/")
def view_index():
    return render_template("index.html")
