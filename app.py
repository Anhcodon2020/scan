from flask import Flask, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
import os

app = Flask(__name__)

# Cấu hình Database (Lấy từ biến môi trường hoặc dùng mặc định)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///:memory:')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

@app.route('/healthz')
def health_check():
    try:
        # Thực hiện một truy vấn nhẹ để kiểm tra kết nối
        db.session.execute(text('SELECT 1'))
        return jsonify({'status': 'healthy', 'database': 'connected'}), 200
    except Exception as e:
        # Nếu lỗi, in ra log và trả về lỗi 500 cho Render
        print(f"Health check failed: {e}")
        return jsonify({'status': 'unhealthy', 'database': 'disconnected', 'error': str(e)}), 500

if __name__ == '__main__':
    # Chạy app
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))