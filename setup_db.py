import os
from dotenv import load_dotenv

# Nạp biến môi trường trước khi import app
load_dotenv()

from app import app
from extensions import db
from models.users import User
from models.load import Load

def deploy():
    print("--- Đang khởi tạo Database trên Aiven ---")
    with app.app_context():
        # 1. Tạo tất cả các bảng (users, v.v.)
        try:
            db.create_all()
            print("✅ Đã tạo bảng thành công!")
        except Exception as e:
            print(f"❌ Lỗi tạo bảng: {e}")
            return

        # 2. Tạo tài khoản admin mặc định
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', password='123', role='admin')
            db.session.add(admin)
            db.session.commit()
            print("✅ Đã tạo user: admin / 123")
        else:
            print("ℹ️ User 'admin' đã tồn tại.")

if __name__ == "__main__":
    deploy()