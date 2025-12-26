import os
from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

app = Flask(__name__)

# Cấu hình Database
# Lấy URL từ biến môi trường DATABASE_URL, nếu không có thì dùng SQLite local để test
db_url = os.environ.get('DATABASE_URL', 'sqlite:///local.db')
# Fix lỗi tương thích: SQLAlchemy yêu cầu 'postgresql://', nhưng một số dịch vụ trả về 'postgres://'
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
# Tự động chuyển đổi mysql:// thành mysql+pymysql:// để sử dụng driver pymysql
if db_url and db_url.startswith("mysql://"):
    db_url = db_url.replace("mysql://", "mysql+pymysql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

@app.route('/')
def home():
    # Render trang home.html
    return render_template('home.html', user="Bạn Mới")

@app.route('/contact')
def contact():
    # Render trang contact.html
    return render_template('contact.html')

@app.route('/test-db')
def test_db():
    try:
        # Thực hiện một truy vấn đơn giản để kiểm tra kết nối
        db.session.execute(text('SELECT 1'))
        return "Kết nối Database thành công!", 200
    except Exception as e:
        return f"Lỗi kết nối Database: {str(e)}", 500

if __name__ == '__main__':
    app.run(debug=True)
