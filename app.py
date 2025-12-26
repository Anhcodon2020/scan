from flask import Flask, render_template

app = Flask(__name__)

@app.route('/')
def home():
    # Render trang home.html
    return render_template('home.html', user="Bạn Mới")

@app.route('/contact')
def contact():
    # Render trang contact.html
    return render_template('contact.html')

if __name__ == '__main__':
    app.run(debug=True)
