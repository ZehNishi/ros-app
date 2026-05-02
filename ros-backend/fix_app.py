import re

with open('/home/jose/ros-app/ros-backend/static/js/app.js', 'r') as f:
    content = f.read()

new_content = re.sub(
    r"document\.getElementById\(`([^`]+-\$\{this\.id\})`\)",
    r"this._el.querySelector(`#\1`)",
    content
)

with open('/home/jose/ros-app/ros-backend/static/js/app.js', 'w') as f:
    f.write(new_content)

print("Fixed app.js")
