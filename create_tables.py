from app import app
from extensions import db

# Script này giúp tạo bảng 'load' và các bảng khác nếu chưa có trên database
if __name__ == "__main__":
    print("--- Đang kiểm tra và cập nhật Database ---")
    with app.app_context():
        db.create_all()
        print("✅ Đã tạo bảng 'load' (và các bảng thiếu) thành công!")